#!/usr/bin/env python3
"""Generate the "Every lane" tables of specs orchestration/lane-registry.md (REGISTRY-GEN-1, T416).

Sources, all read with GH_TOKEN (read-only):
  - purpusgit/lanes issue titles  -> Seat code and Role, for titles exactly `CODE-NN · role`;
  - purpusgit/specs open PRs on `inbox/*` branches -> the Inbox column, matched by code;
  - lanes comments from the last 7 days containing `Seat:` (a seat card) -> Status.

Status (non-terminal rows): inbox + seat card < 7 days -> `live`; no inbox + fresh card -> `cowork`;
no fresh card -> `dormant`; closed issue -> `closed`. Rows whose status is a hand-written terminal note
(moved, retired, archived, retiring, vacant, unconfirmed, closed) or whose seat is `none` are kept
verbatim, as is every "Former title" and all prose outside the tables. An open lane issue titled
`CODE-NN · role` with no row gets one; a new workstream code gets a new section at the end.

Usage: lane_registry_gen.py <path to lane-registry.md>   (rewrites the file in place, then audits)
       lane_registry_gen.py --self-check                 (offline test of the render step)
Exit 1 when the audit has findings other than the accepted moved/retired rows (lanes #68, #178, #154).
"""
import collections
import json
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
ROW_RE = re.compile(r"^\| (`[A-Z0-9]+-\d+`|none) \| ([^|]*)\| ([^|]*)\| ([^|]*)\| ([^|]*)\| ?(.*?) ?\|\s*$")
TERMINAL = ("moved", "retired", "archived", "retiring", "vacant", "unconfirmed", "closed")
ACCEPTED_DUP_ISSUES = {68, 178, 154}
FRESH_DAYS = 7
HEADER = ["| Seat | Role | Issue | Inbox | Status | Former title |", "|---|---|---|---|---|---|"]


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
    """CODE-NN an inbox PR belongs to: from its branch (inbox/<code-nn>), else from its title."""
    m = re.fullmatch(r"inbox/([a-z]+-\d{2})", pr["head"]["ref"], re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([A-Z]+-\d{2})\b", pr["title"])
    return m.group(1) if m else None


def fetch():
    issues = {i["number"]: i for i in gh_all(f"/repos/{ORG}/lanes/issues", {"state": "all"}) if "pull_request" not in i}
    inboxes = {}
    for pr in gh_all(f"/repos/{ORG}/specs/pulls", {"state": "open"}):
        code = pr["head"]["ref"].lower().startswith("inbox/") and inbox_code(pr)
        if code:
            inboxes.setdefault(code, pr["number"])  # oldest-listed wins; duplicates surface in the audit
    since = (datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = {int(c["issue_url"].rsplit("/", 1)[1]) for c in gh_all(f"/repos/{ORG}/lanes/issues/comments", {"since": since})
             if "Seat:" in (c.get("body") or "") and c["created_at"] >= since}
    return issues, inboxes, fresh


def parse_rows(lines):
    rows = []
    for idx, line in enumerate(lines):
        m = ROW_RE.match(line)
        if not m:
            continue
        iss = re.search(r"\[#(\d+)\]", m.group(3))
        rows.append(dict(line=idx, seat=m.group(1).strip("`"), role=m.group(2).strip(), issue_cell=m.group(3).strip(),
                         issue=int(iss.group(1)) if iss else None, inbox_cell=m.group(4).strip(),
                         status=m.group(5).strip(), former=m.group(6).strip()))
    return rows


def is_terminal(r):
    return r["seat"] == "none" or r["status"].startswith(TERMINAL)


def update_row(r, issues, inboxes, fresh):
    if is_terminal(r) or r["issue"] is None:
        return r
    i = issues.get(r["issue"])
    if i is None:
        return r
    m = TITLE_RE.match(i["title"].strip())
    if m:
        r["seat"], r["role"] = f"{m.group(1)}-{m.group(2)}", m.group(3)
    n = inboxes.get(r["seat"])
    r["inbox_cell"] = f"[specs#{n}](https://github.com/{ORG}/specs/pull/{n})" if n else ""
    if i["state"] == "closed":
        r["status"] = "closed"
    elif r["issue"] in fresh:
        r["status"] = "live" if n else "cowork"
    else:
        r["status"] = "dormant"
    return r


def fmt(r):
    seat = "none" if r["seat"] == "none" else f"`{r['seat']}`"
    cells = [seat, r["role"], r["issue_cell"], r["inbox_cell"], r["status"], r["former"]]
    return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|"


def seat_key(r):
    m = re.match(r"([A-Z]+)-(\d+)", r["seat"])
    return (m.group(1), int(m.group(2)), r["issue"] or 0) if m else ("~", 999, r["issue"] or 0)


def render(text, issues, inboxes, fresh):
    """Rewrite each workstream section's table under '## Every lane'; everything else is kept as is."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("## Every lane"))
    head, body = lines[:start], lines[start:]
    rows = [update_row(r, issues, inboxes, fresh) for r in parse_rows(body)]
    known = {r["issue"] for r in rows}
    for n, i in sorted(issues.items()):
        m = TITLE_RE.match(i["title"].strip())
        if m and i["state"] == "open" and n not in known:
            r = dict(seat=f"{m.group(1)}-{m.group(2)}", role=m.group(3), issue_cell=f"[#{n}](https://github.com/{ORG}/lanes/issues/{n})",
                     issue=n, inbox_cell="", status="", former="—", line=None)
            rows.append(update_row(r, issues, inboxes, fresh))
    by_ws = collections.defaultdict(list)
    for r in rows:
        ws = r["seat"].split("-")[0] if r["seat"] != "none" else None
        by_ws[ws].append(r)
    out, ws, emitted = [], None, set()
    for idx, line in enumerate(body):
        h = re.match(r"^### `([A-Z]+)`", line)
        if h:
            ws = h.group(1)
        if ROW_RE.match(line) or line in HEADER:
            if line == HEADER[0] and ws not in emitted:
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


def audit(text, issues, inboxes):
    findings = []
    rows = [r for r in parse_rows(text.split("\n")) if r["seat"] != "none"]
    for key in ("seat", "issue", "inbox_cell"):
        c = collections.Counter(r[key] for r in rows if r[key])
        for k, v in c.items():
            if v > 1 and not (key == "issue" and k in ACCEPTED_DUP_ISSUES):
                findings.append(f"DUP {key.upper()} {k}")
    for r in rows:
        i = issues.get(r["issue"])
        if i and not is_terminal(r) and i["title"].strip() != f"{r['seat']} · {r['role']}":
            findings.append(f"TITLE {r['seat']} {i['title']!r}")
    listed = {r["seat"] for r in rows if r["inbox_cell"]}
    for code, n in sorted(inboxes.items()):
        if code not in listed:
            findings.append(f"ORPHAN-INBOX specs#{n} ({code})")
    seen = {r["issue"] for r in parse_rows(text.split("\n"))}
    for n, i in sorted(issues.items()):
        if n not in seen and i["state"] == "open" and any(l["name"] == "lane" for l in i.get("labels", [])):
            findings.append(f"LANE-NOT-IN-REGISTRY #{n} {i['title'][:60]!r}")
    return findings


def self_check():
    reg = "\n".join([
        "# Lane registry", "", "## Every lane", "", "### `GIT` · Git gates", "", "Prose kept.", "", *HEADER,
        "| `GIT-00` | master | [#59](u) | | live | old master |",
        "| `GIT-02` | executor | [#2](u) | [specs#1](x) | live | LANE: git gates |",
        "| `GIT-03` | executor | [#9](u) | | dormant | LANE: shared |",
        "| none | executor | [#43](u) | | retired | code freed |",
        "| `GIT-04` | executor | [#68](u) | | moved | now `X-03` |", "",
    ])
    iss = {59: dict(title="GIT-00 · master", state="open", labels=[]), 2: dict(title="GIT-02 · executor", state="open", labels=[]),
           9: dict(title="GIT-03 · executor", state="closed", labels=[]), 68: dict(title="X-03 · executor", state="open", labels=[]),
           300: dict(title="GIT-05 · executor", state="open", labels=[{"name": "lane"}]),
           301: dict(title="NEW-00 · master", state="open", labels=[{"name": "lane"}])}
    out = render(reg, iss, {"GIT-02": 412, "GIT-05": 500}, fresh={2, 59, 300})
    want = [
        "| `GIT-00` | master | [#59](u) | | cowork | old master |",
        "| `GIT-02` | executor | [#2](u) | [specs#412](https://github.com/purpusgit/specs/pull/412) | live | LANE: git gates |",
        "| `GIT-03` | executor | [#9](u) | | closed | LANE: shared |",
        "| `GIT-04` | executor | [#68](u) | | moved | now `X-03` |",
        "| `GIT-05` | executor | [#300](https://github.com/purpusgit/lanes/issues/300) | [specs#500](https://github.com/purpusgit/specs/pull/500) | live | — |",
        "| none | executor | [#43](u) | | retired | code freed |",
        "Prose kept.", "### `NEW` · (new workstream",
        "| `NEW-00` | master | [#301](https://github.com/purpusgit/lanes/issues/301) | | dormant | — |",
    ]
    for w in want:
        assert w in out, f"missing: {w}\n---\n{out}"
    assert out.index("`GIT-04`") < out.index("`GIT-05`") < out.index("| none |"), out
    assert out.count(HEADER[0]) == 2, out
    assert render(out, iss, {"GIT-02": 412, "GIT-05": 500}, fresh={2, 59, 300}) == out, "render is not idempotent"
    assert audit(out, iss, {"GIT-02": 412, "GIT-05": 500, "ZZ-02": 9}) == ["ORPHAN-INBOX specs#9 (ZZ-02)"], audit(out, iss, {"ZZ-02": 9})
    print("lane_registry_gen self-check GREEN")


def main():
    if sys.argv[1:] == ["--self-check"]:
        return self_check()
    if len(sys.argv) != 2 or not TOKEN:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    issues, inboxes, fresh = fetch()
    text = open(path, encoding="utf-8").read()
    new = render(text, issues, inboxes, fresh)
    open(path, "w", encoding="utf-8").write(new)
    rows = parse_rows(new.split("\n"))
    print(f"{len(rows)} rows · {len(inboxes)} open inboxes · {len(fresh)} lanes with a seat card in {FRESH_DAYS} days · "
          f"status {dict(collections.Counter(r['status'].split(' ')[0] for r in rows))}")
    findings = audit(new, issues, inboxes)
    for f in findings:
        print("AUDIT", f)
    print(f"audit: {len(findings)} finding(s)")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
