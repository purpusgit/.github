#!/usr/bin/env python3
"""Generate the "Every lane" tables of specs orchestration/lane-registry.md (REGISTRY-GEN-1, T416).

Follows INBOX-ONLY-1 (spec-inbox-only-seats.md section 5 step 7). Sources, all read with GH_TOKEN (read-only):
  - purpusgit/specs `inbox/*` PRs -> one row per seat. Seat code from the branch, role from a title
    `CODE-NN inbox (<role>, <workstream>)`, surface from the card's `Surface:` field;
  - each open inbox's comments -> "Last signed line": time, link and first line of the newest `### CODE-NN:` comment;
  - each closed inbox's comments -> the retirement marker (`### CODE-NN: retired` or `### XX-NN: CODE-NN retired`);
  - purpusgit/lanes issues titled `CODE-NN · role` -> history only, for a seat with no inbox;
  - lanes comments from the last 7 days containing `Seat:` -> Status of a seat with no inbox.

Status: an open inbox -> `live` (`cowork` when the card says `Surface: Cowork`), whatever the lane's state.
Closed inbox with a marker and no open one -> `retired`. No inbox: lane + seat card < 7 days -> `cowork`, no fresh card
-> `dormant`, closed lane -> `closed`. Hand-written `moved`/`vacant`/`archived`/`unconfirmed` rows and `none` rows are
kept verbatim; without an inbox so are the other terminal notes. "Former title" and all prose outside the tables are
kept. A seat with an open inbox and no row gets one; a new workstream code gets a new section at the end.

Usage: lane_registry_gen.py <path to lane-registry.md>   (rewrites the file in place, then audits)
       lane_registry_gen.py --self-check                 (offline test of the render and audit steps)
Exit 1 when the audit has findings other than the accepted moved/retired rows (lanes #68, #178, #154).
"""
import collections
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ORG = "purpusgit"
API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN", "")
TITLE_RE = re.compile(r"^([A-Z]+)-(\d{2}) · (master|orchestrator|executor|hub)$")
INBOX_TITLE_RE = re.compile(r"^([A-Z]+-\d{2}) inbox \(([^,()]+), ([^()]+)\)$")
SURFACE_RE = re.compile(r"Surface:\W*(Claude Code|Cowork)", re.I)
# 5 fixed cells, the optional "Last signed line" cell (added by this generator), then Former title.
ROW_RE = re.compile(r"^\| (`[A-Z0-9]+-\d+`|none) \| ([^|]*)\| ([^|]*)\| ([^|]*)\| ([^|]*)\|(?: ([^|]*)\|)? ?(.*?) ?\|\s*$")
TERMINAL = ("moved", "retired", "archived", "retiring", "vacant", "unconfirmed", "closed")
KEEP = ("moved", "vacant", "archived", "unconfirmed")  # hand-written even when the seat has an open inbox
ACCEPTED_DUP_ISSUES = {68, 178, 154}
FRESH_DAYS = 7
CUTOVER = "2026-10-24T03:00:00Z"  # INBOX-ONLY-1 cut-over (C): no lane issue may be opened after it
HEADER = ["| Seat | Role | Issue | Inbox | Status | Last signed line | Former title |", "|---|---|---|---|---|---|---|"]
OLD_HEADER = ["| Seat | Role | Issue | Inbox | Status | Former title |", "|---|---|---|---|---|---|"]


def gh(path, params=None, _retries=3):
    url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    last = None
    for attempt in range(_retries):
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {TOKEN}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"[]")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"GET {path} -> {e.code}")  # a failed read must never render as an empty registry
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.5 * (2 ** attempt))
    raise SystemExit(f"GET {path}: network failure after {_retries} attempts: {last!r}")


def gh_all(path, params=None):
    params, out, page = dict(params or {}, per_page=100), [], 1
    while True:
        batch = gh(path, dict(params, page=page))
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def inbox_code(pr):
    """CODE-NN an inbox PR belongs to: from its branch (inbox/<code-nn>), else from its title.
    A legacy branch inbox/lane-<n> names the lane, not the seat (INBOX-ONLY-1 section 5 step 6)."""
    m = re.fullmatch(r"inbox/([a-z]+-\d{2})", pr["head"]["ref"], re.I)
    if m and not m.group(1).lower().startswith("lane-"):
        return m.group(1).upper()
    m = re.search(r"\b([A-Z]+-\d{2})\b", pr["title"])
    return m.group(1) if m else None


def signed_by(c, code):
    return (c.get("body") or "").lstrip().startswith(f"### {code}:")


def newest_signed(code, n, count):
    """Newest `### CODE-NN:` comment on inbox n; looks back at most 300 comments."""
    last = max(1, math.ceil(count / 100))
    for page in range(last, max(0, last - 3), -1):
        for c in reversed(gh(f"/repos/{ORG}/specs/issues/{n}/comments", {"per_page": 100, "page": page})):
            if signed_by(c, code):
                return c
    return None


def glance(c):
    if not c:
        return ""
    line = c["body"].lstrip().split("\n", 1)[0].split(":", 1)[1].strip().replace("|", "/")
    line = line if len(line) <= 90 else line[:89] + "…"
    return f"[{c['created_at'][:16].replace('T', ' ')}]({c['html_url']}) {line}"


def inbox_info(prs, comment):
    """One seat's open inbox(es), oldest first: the first is the seat's inbox, any other is a duplicate."""
    pr = prs[0]
    m = INBOX_TITLE_RE.match(pr["title"].strip())
    sf = SURFACE_RE.search(pr.get("body") or "")
    return dict(n=pr["number"], title=pr["title"], role=m.group(2).strip() if m else None,
                surface=sf.group(1).title() if sf else None, label=any(l["name"] == "inbox" for l in pr.get("labels", [])),
                glance=glance(comment), dups=[p["number"] for p in prs[1:]])


def is_retired(code, comments):
    marker = re.compile(rf"### (?:{re.escape(code)}: retired|[A-Z]+-\d{{2}}: {re.escape(code)} retired)\b")
    return any(marker.match((c.get("body") or "").lstrip()) for c in comments)


def fetch():
    issues = {i["number"]: i for i in gh_all(f"/repos/{ORG}/lanes/issues", {"state": "all"}) if "pull_request" not in i}
    open_ib, closed_ib = collections.defaultdict(list), collections.defaultdict(list)
    for pr in gh_all(f"/repos/{ORG}/specs/pulls", {"state": "all"}):
        code = pr["head"]["ref"].lower().startswith("inbox/") and inbox_code(pr)
        if code:
            (open_ib if pr["state"] == "open" else closed_ib)[code].append(pr)
    counts = {i["number"]: i["comments"] for i in gh_all(f"/repos/{ORG}/specs/issues", {"state": "open"}) if "pull_request" in i}
    inboxes = {}
    for code, prs in open_ib.items():
        prs.sort(key=lambda p: p["number"])
        inboxes[code] = inbox_info(prs, newest_signed(code, prs[0]["number"], counts.get(prs[0]["number"], 0)))
    retired, unmarked = set(), {}
    for code, prs in closed_ib.items():
        if code in inboxes:
            continue  # a superseded duplicate; the seat's inbox is open
        if any(is_retired(code, gh_all(f"/repos/{ORG}/specs/issues/{p['number']}/comments")) for p in prs):
            retired.add(code)
        else:
            unmarked[code] = max(p["number"] for p in prs)
    since = (datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = {int(c["issue_url"].rsplit("/", 1)[1]) for c in gh_all(f"/repos/{ORG}/lanes/issues/comments", {"since": since})
             if "Seat:" in (c.get("body") or "") and c["created_at"] >= since}
    return issues, inboxes, fresh, retired, unmarked


def parse_rows(lines):
    rows = []
    for idx, line in enumerate(lines):
        m = ROW_RE.match(line)
        if not m:
            continue
        iss = re.search(r"\[#(\d+)\]", m.group(3))
        rows.append(dict(line=idx, seat=m.group(1).strip("`"), role=m.group(2).strip(), issue_cell=m.group(3).strip(),
                         issue=int(iss.group(1)) if iss else None, inbox_cell=m.group(4).strip(),
                         status=m.group(5).strip(), glance=(m.group(6) or "").strip(), former=m.group(7).strip()))
    return rows


def update_row(r, issues, inboxes, fresh, retired=()):
    if r["seat"] == "none" or r["status"].startswith(KEEP):
        return r
    ib = inboxes.get(r["seat"])
    if ib:  # INBOX-ONLY-1: the inbox decides, whatever the lane's state
        r["inbox_cell"] = f"[specs#{ib['n']}](https://github.com/{ORG}/specs/pull/{ib['n']})"
        r["role"], r["glance"] = ib["role"] or r["role"], ib["glance"]
        r["status"] = "cowork" if ib["surface"] == "Cowork" else "live"
        return r
    if r["seat"] in retired:
        r["status"] = "retired"
        return r
    i = issues.get(r["issue"])
    if r["status"].startswith(TERMINAL) or i is None:
        return r
    m = TITLE_RE.match(i["title"].strip())
    if m:
        r["seat"], r["role"] = f"{m.group(1)}-{m.group(2)}", m.group(3)
    r["inbox_cell"] = ""
    if i["state"] == "closed":
        r["status"] = "closed"
    else:
        r["status"] = "cowork" if r["issue"] in fresh else "dormant"
    return r


def fmt(r):
    seat = "none" if r["seat"] == "none" else f"`{r['seat']}`"
    cells = [seat, r["role"], r["issue_cell"], r["inbox_cell"], r["status"], r.get("glance", ""), r["former"]]
    return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|"


def seat_key(r):
    m = re.match(r"([A-Z]+)-(\d+)", r["seat"])
    return (m.group(1), int(m.group(2)), r["issue"] or 0) if m else ("~", 999, r["issue"] or 0)


def render(text, issues, inboxes, fresh, retired=()):
    """Rewrite each workstream section's table under '## Every lane'; everything else is kept as is."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("## Every lane"))
    head, body = lines[:start], lines[start:]
    rows = [update_row(r, issues, inboxes, fresh, retired) for r in parse_rows(body)]
    lane_of = {}
    for n, i in sorted(issues.items()):
        m = TITLE_RE.match(i["title"].strip())
        if m:
            lane_of.setdefault(f"{m.group(1)}-{m.group(2)}", (n, m.group(3)))
    seats = {r["seat"] for r in rows}
    for code in sorted(set(inboxes) - seats):  # inbox-only seat, or a seat whose lane never had a row
        n, role = lane_of.get(code, (None, ""))
        r = dict(seat=code, role=role, issue_cell=f"[#{n}](https://github.com/{ORG}/lanes/issues/{n})" if n else "",
                 issue=n, inbox_cell="", status="", glance="", former="—", line=None)
        rows.append(update_row(r, issues, inboxes, fresh, retired))
    known, seats = {r["issue"] for r in rows}, {r["seat"] for r in rows}
    for n, i in sorted(issues.items()):
        m = TITLE_RE.match(i["title"].strip())
        if m and i["state"] == "open" and n not in known and f"{m.group(1)}-{m.group(2)}" not in seats:
            r = dict(seat=f"{m.group(1)}-{m.group(2)}", role=m.group(3), issue_cell=f"[#{n}](https://github.com/{ORG}/lanes/issues/{n})",
                     issue=n, inbox_cell="", status="", glance="", former="—", line=None)
            rows.append(update_row(r, issues, inboxes, fresh, retired))
    by_ws = collections.defaultdict(list)
    for r in rows:
        ws = r["seat"].split("-")[0] if r["seat"] != "none" else None
        by_ws[ws].append(r)
    out, ws, emitted = [], None, set()
    for idx, line in enumerate(body):
        h = re.match(r"^### `([A-Z]+)`", line)
        if h:
            ws = h.group(1)
        if ROW_RE.match(line) or line in HEADER + OLD_HEADER:
            if line in (HEADER[0], OLD_HEADER[0]) and ws not in emitted:
                emitted.add(ws)
                # 'none' rows stay in the section they were written in
                nones = [r for r in rows if r["seat"] == "none" and _section_of(body, r["line"]) == ws]
                out += HEADER + [fmt(r) for r in sorted(by_ws.get(ws, []), key=seat_key) + nones]
            continue
        out.append(line)
    for ws in sorted(k for k in by_ws if k and k not in emitted):
        out += ["", f"### `{ws}` · (new workstream, name it in the Workstreams table)", ""] + HEADER + [fmt(r) for r in sorted(by_ws[ws], key=seat_key)]
    return "\n".join(head + out)


def _section_of(body, idx):
    for l in reversed(body[: (idx or 0) + 1]):
        h = re.match(r"^### `([A-Z]+)`", l)
        if h:
            return h.group(1)
    return None


def audit(text, issues, inboxes, unmarked=None):
    findings = []
    rows = [r for r in parse_rows(text.split("\n")) if r["seat"] != "none"]
    for key in ("seat", "issue", "inbox_cell"):
        c = collections.Counter(r[key] for r in rows if r[key])
        for k, v in c.items():
            if v > 1 and not (key == "issue" and k in ACCEPTED_DUP_ISSUES):
                findings.append(f"DUP {key.upper()} {k}")
    listed = {r["seat"] for r in rows if r["inbox_cell"]}
    for code, ib in sorted(inboxes.items()):
        m = INBOX_TITLE_RE.match(ib["title"].strip())
        if not m or m.group(1) != code:
            findings.append(f"TITLE specs#{ib['n']} {ib['title']!r}")
        if not ib["label"]:
            findings.append(f"INBOX-NO-LABEL specs#{ib['n']} ({code})")
        if ib["dups"]:
            findings.append(f"DUP INBOX {code} specs#{ib['n']} " + " ".join(f"specs#{d}" for d in ib["dups"]))
        if code not in listed:
            findings.append(f"ORPHAN-INBOX specs#{ib['n']} ({code})")
    for code, n in sorted((unmarked or {}).items()):
        findings.append(f"INBOX-CLOSED specs#{n} ({code}) closed without a retirement line")
    for n, i in sorted(issues.items()):
        if i.get("created_at", "") > CUTOVER and any(l["name"] == "lane" for l in i.get("labels", [])):
            findings.append(f"LANE-OPENED-AFTER-C #{n} {i['title'][:60]!r}")
    return findings


def self_check():
    reg = "\n".join([
        "# Lane registry", "", "## Every lane", "", "### `GIT` · Git gates", "", "Prose kept.", "", *OLD_HEADER,
        "| `GIT-00` | master | [#59](u) | | live | old master |",
        "| `GIT-02` | executor | [#2](u) | [specs#1](x) | dormant | LANE: git gates |",
        "| `GIT-03` | executor | [#9](u) | | closed | LANE: shared |",
        "| `GIT-06` | executor | [#10](u) | | live | LANE: gone |",
        "| `GIT-07` | executor | | [specs#7](x) | live | new |",
        "| none | executor | [#43](u) | | retired | code freed |",
        "| `GIT-04` | executor | [#68](u) | | moved | now `X-03` |", "",
    ])
    iss = {59: dict(title="GIT-00 · master", state="open", labels=[]), 2: dict(title="GIT-02 · executor", state="open", labels=[]),
           9: dict(title="GIT-03 · executor", state="closed", labels=[]), 10: dict(title="GIT-06 · executor", state="closed", labels=[]),
           68: dict(title="X-03 · executor", state="open", labels=[]),
           300: dict(title="GIT-05 · executor", state="open", labels=[{"name": "lane"}], created_at="2026-10-20T00:00:00Z"),
           301: dict(title="NEW-00 · master", state="open", labels=[{"name": "lane"}], created_at="2026-10-24T05:00:00Z")}
    lbl = [{"name": "inbox"}]
    card = "GIT-02 · executor · Git gates · Seat: s (since d) · Surface: Claude Code · Master: GIT-01"
    said = dict(body="### GIT-02: did T416 | re-run, link; next x\nmore", created_at="2026-10-24T20:50:16Z", html_url="c")
    inboxes = {
        "GIT-02": inbox_info([dict(number=412, title="GIT-02 inbox (executor, Git gates)", body=card, labels=lbl)], said),
        "GIT-03": inbox_info([dict(number=413, title="inbox: GIT-03 · executor", body="", labels=lbl)], None),
        "GIT-05": inbox_info([dict(number=500, title="GIT-05 inbox (executor, Git gates)", body="", labels=lbl),
                              dict(number=501, title="GIT-05 inbox (executor, Git gates)", body="", labels=lbl)], None),
        "GIT-08": inbox_info([dict(number=520, title="GIT-08 inbox (reviewer, Git gates)", body="**Surface:** Cowork", labels=[])], None),
    }
    assert inbox_code(dict(head=dict(ref="inbox/lane-31"), title="Inbox: CHAT-05 (lane 31)")) == "CHAT-05"
    assert inbox_code(dict(head=dict(ref="inbox/git-02"), title="x")) == "GIT-02"
    assert signed_by(said, "GIT-02") and not signed_by(dict(body="### GIT-01 → GIT-02: go"), "GIT-02")
    assert is_retired("GIT-07", [dict(body="### GIT-01: GIT-07 retired")]) and not is_retired("GIT-07", [dict(body="### GIT-07: retiring soon")])
    out = render(reg, iss, inboxes, fresh={2, 59, 300}, retired={"GIT-07"})
    want = [
        # lane, no inbox: keeps its lane row
        "| `GIT-00` | master | [#59](u) | | cowork | | old master |",
        "| `GIT-02` | executor | [#2](u) | [specs#412](https://github.com/purpusgit/specs/pull/412) | live | "
        "[2026-10-24 20:50](c) did T416 / re-run, link; next x | LANE: git gates |",
        # open inbox + closed lane: live, never closed
        "| `GIT-03` | executor | [#9](u) | [specs#413](https://github.com/purpusgit/specs/pull/413) | live | | LANE: shared |",
        "| `GIT-06` | executor | [#10](u) | | closed | | LANE: gone |",
        "| `GIT-07` | executor | | [specs#7](x) | retired | | new |",
        "| `GIT-04` | executor | [#68](u) | | moved | | now `X-03` |",
        "| `GIT-05` | executor | [#300](https://github.com/purpusgit/lanes/issues/300) | [specs#500](https://github.com/purpusgit/specs/pull/500) | live | | — |",
        # inbox only: gets a row, surface from the card
        "| `GIT-08` | reviewer | | [specs#520](https://github.com/purpusgit/specs/pull/520) | cowork | | — |",
        "| none | executor | [#43](u) | | retired | | code freed |",
        "Prose kept.", "### `NEW` · (new workstream",
        "| `NEW-00` | master | [#301](https://github.com/purpusgit/lanes/issues/301) | | dormant | | — |",
    ]
    for w in want:
        assert w in out, f"missing: {w}\n---\n{out}"
    assert out.index("`GIT-04`") < out.index("`GIT-05`") < out.index("`GIT-08`") < out.index("| none |"), out
    assert out.count(HEADER[0]) == 2 and OLD_HEADER[0] not in out, out
    assert render(out, iss, inboxes, fresh={2, 59, 300}, retired={"GIT-07"}) == out, "render is not idempotent"
    got = audit(out, iss, dict(inboxes, ZZ_02=dict(inboxes["GIT-02"], n=9, title="ZZ-02 inbox (executor, Z)")), {"GIT-09": 7})
    assert got == [
        "TITLE specs#413 'inbox: GIT-03 · executor'",
        "DUP INBOX GIT-05 specs#500 specs#501",
        "INBOX-NO-LABEL specs#520 (GIT-08)",
        "TITLE specs#9 'ZZ-02 inbox (executor, Z)'", "ORPHAN-INBOX specs#9 (ZZ_02)",
        "INBOX-CLOSED specs#7 (GIT-09) closed without a retirement line",
        "LANE-OPENED-AFTER-C #301 'NEW-00 · master'",
    ], got
    print("lane_registry_gen self-check GREEN")


def main():
    if sys.argv[1:] == ["--self-check"]:
        return self_check()
    if len(sys.argv) != 2 or not TOKEN:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    issues, inboxes, fresh, retired, unmarked = fetch()
    text = open(path, encoding="utf-8").read()
    new = render(text, issues, inboxes, fresh, retired)
    open(path, "w", encoding="utf-8").write(new)
    rows = parse_rows(new.split("\n"))
    print(f"{len(rows)} rows · {len(inboxes)} open inboxes · {len(retired)} retired by marker · "
          f"{len(fresh)} lanes with a seat card in {FRESH_DAYS} days · "
          f"status {dict(collections.Counter(r['status'].split(' ')[0] for r in rows))}")
    findings = audit(new, issues, inboxes, unmarked)
    for f in findings:
        print("AUDIT", f)
    print(f"audit: {len(findings)} finding(s)")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
