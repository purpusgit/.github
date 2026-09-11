#!/usr/bin/env python3
"""Auth-coverage drift detector.

Scheduled monitor (purpusgit/.github). For every repo in the gated set
(scripts/auth_coverage_gated_repos.json) it asserts three things that can each
rot silently, with no error anywhere, and none of which any check watched
before:

  (a) reporter present   -- a workflow on the repo's default branch has a job
                            whose reported check context == the expected one
                            (`auth-coverage`, or `auth-guard` for the FastAPI
                            service). Catches "gate workflow deleted/renamed".
  (b) requirement present -- the effective branch rules require that context
                            (org-inherited OR repo-level). Catches the Rule 96
                            rename fallout: a renamed repo drops out of a
                            name-list org ruleset and its RequiredStatusChecks
                            go null, with no error.
  (c) context match       -- what the repo reports == what is required; a repo
                            reporting `auth-guard` while a rule requires
                            `auth-coverage` (or vice versa) is required-on-an-
                            absence and blocks nothing real.

NO VACUOUS GREEN. Any check that cannot RUN (API read fails, repo gone,
workflow unparseable) reports UNKNOWN and fails the run -- never "ok".

DELIBERATELY NOT COVERED, stated once: reading org-level rulesets
(GET /orgs/{org}/rulesets) needs org-admin scope the scheduled token does not
have (verified 2026-09-11: 404). Part (b) therefore reads each repo's
EFFECTIVE branch rules (GET /repos/{org}/{repo}/rules/branches/{branch}),
which DO include org-inherited requirements -- so (b)/(c) are ground truth per
repo regardless of scope. What the missing scope costs is only the refinement
of confirming whether the org auth ruleset targets by name-list (fragile to
rename) or by custom property (robust) -- see
Org_AuthCoverage_Ruleset_ByProperty.md. That gap is flagged in every report,
never silently passed.

Read-only against every repo. The only writes are to one tracking issue on
purpusgit/lanes (issues:write, that repo only) -- least privilege, the same
two-token pattern as pushed_but_not_landed_surfacer.py.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "auth_coverage_gated_repos.json")

TOKEN = os.environ.get("GH_TOKEN", "")
# The report comment/issue uses a SEPARATE, narrowly-scoped write token
# (issues:write on purpusgit/lanes only); the org-wide read token has no write
# scope at all. Falls back to TOKEN for local/manual runs with one token.
WRITE_TOKEN = os.environ.get("GH_WRITE_TOKEN", TOKEN)


def gh(method, path, params=None, body=None, _retries=3, token=None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    last_exc = None
    for attempt in range(_retries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token or TOKEN}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"message": raw}
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))
    return 0, {"message": f"transient network failure after {_retries} attempts: {last_exc!r}"}


def reported_contexts(wf):
    """Bare check contexts a workflow's jobs produce. A job's context is its
    `name` if set, else its job-id key. A reusable-workflow caller (job-level
    `uses:`) produces compound `<job> / <reusable job>` contexts, never a bare
    one, so it cannot satisfy a bare required context -- skip it. This matches
    how the m34 action is deliberately built (composite action inside a plain
    job, no job `name:`, so the reported context is the bare job key)."""
    out = []
    jobs = (wf or {}).get("jobs")
    if not isinstance(jobs, dict):
        return out
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or "uses" in job:
            continue
        out.append(str(job.get("name") or job_id))
    return out


def check_reporter(org, repo, branch, expected):
    """(a) -> (status, detail); status in {ok, missing, unknown}."""
    st, listing = gh("GET", f"/repos/{org}/{repo}/contents/.github/workflows",
                     params={"ref": branch})
    if st == 404:
        return "missing", "no .github/workflows on the default branch"
    if st != 200 or not isinstance(listing, list):
        return "unknown", f"could not list workflows: HTTP {st}"
    matched, errs = [], []
    for e in listing:
        name = e.get("name", "")
        if e.get("type") != "file" or not name.endswith((".yml", ".yaml")):
            continue
        fst, fd = gh("GET", f"/repos/{org}/{repo}/contents/{e['path']}",
                     params={"ref": branch})
        if fst != 200 or "content" not in fd:
            errs.append(f"{name} (HTTP {fst})")
            continue
        try:
            wf = yaml.safe_load(base64.b64decode(fd["content"]).decode("utf-8", "replace"))
        except Exception as ex:
            errs.append(f"{name} (parse: {ex})")
            continue
        if expected in reported_contexts(wf):
            matched.append(name)
    if matched:
        return "ok", "reported by " + ", ".join(sorted(matched))
    if errs:
        return "unknown", "no matching job; unread/unparsed: " + ", ".join(errs)
    return "missing", "no workflow job reports this context on the default branch"


def check_requirement(org, repo, branch, expected, known_auth):
    """(b)+(c) -> (status, detail); status in {ok, mismatch, missing, unknown}.
    Uses effective branch rules, which include org-inherited requirements, so
    this is ground truth per repo even without org-ruleset read scope."""
    st, rules = gh("GET",
                   f"/repos/{org}/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}")
    if st != 200 or not isinstance(rules, list):
        return "unknown", f"could not read effective branch rules: HTTP {st}"
    required, sources = [], set()
    for r in rules:
        if r.get("type") != "required_status_checks":
            continue
        for c in (r.get("parameters") or {}).get("required_status_checks") or []:
            if c.get("context"):
                required.append(c["context"])
        sources.add(f"{r.get('ruleset_source_type')} {r.get('ruleset_source')} #{r.get('ruleset_id')}")
    if expected in required:
        return "ok", "required by " + "; ".join(sorted(sources))
    others = sorted(c for c in required if c in known_auth and c != expected)
    if others:
        return "mismatch", f"branch requires {others}, but this repo reports '{expected}'"
    if required:
        return "missing", f"'{expected}' not required (branch requires {sorted(set(required))})"
    return "missing", "no required_status_checks apply to this branch"


def combine(reporter_status, requirement_status):
    """Single source of truth for the verdict. Guarded by selftest()."""
    if reporter_status == "unknown" or requirement_status == "unknown":
        return "UNKNOWN", "unverifiable"
    if reporter_status == "missing":
        return "DRIFT", "reporter_missing"
    if requirement_status == "mismatch":
        return "DRIFT", "context_mismatch"
    if requirement_status == "missing":
        return "DRIFT", "requirement_missing"
    return "OK", "ok"


def probe_org_scope(org, known_auth):
    """Best-effort enrichment: if org ruleset read is available, characterise
    whether the auth ruleset targets by name (fragile) or property (robust).
    If not (the expected case), return the scope-gap note for the report."""
    st, data = gh("GET", f"/orgs/{org}/rulesets")
    if st != 200 or not isinstance(data, list):
        return (f"org ruleset read unavailable (HTTP {st} on /orgs/{org}/rulesets). "
                "Part (b) used each repo's effective branch rules (ground truth, "
                "org-inherited requirements included); it could NOT confirm whether "
                "the org auth ruleset targets by name-list (fragile to rename) or by "
                "custom property (robust). See Org_AuthCoverage_Ruleset_ByProperty.md.")
    notes = []
    for rs in data:
        rid = rs.get("id")
        dst, detail = gh("GET", f"/orgs/{org}/rulesets/{rid}")
        if dst != 200:
            continue
        rules = detail.get("rules") or []
        hit = any(r.get("type") == "required_status_checks" and
                  any((c.get("context") in known_auth)
                      for c in (r.get("parameters") or {}).get("required_status_checks") or [])
                  for r in rules)
        if hit:
            cond = detail.get("conditions") or {}
            by = ("custom property (robust)" if "repository_property" in cond
                  else "name-list/pattern (fragile to rename)" if "repository_name" in cond
                  else "unknown")
            notes.append(f"ruleset #{rid} \"{detail.get('name')}\" targets by {by}")
    return "org ruleset read available. " + ("; ".join(notes) if notes
           else "no org ruleset requires a known auth context")


def evaluate(cfg):
    org = cfg["org"]
    known = sorted({g["expected_context"] for g in cfg["gated_repos"]})
    rows = []
    for g in cfg["gated_repos"]:
        repo, expected = g["repo"], g["expected_context"]
        st, meta = gh("GET", f"/repos/{org}/{repo}")
        if st == 404:
            rows.append({"repo": repo, "branch": "-", "expected": expected,
                         "reporter": ("missing", "repo not found -- renamed or deleted; update the data file"),
                         "requirement": ("-", "-"), "verdict": "DRIFT", "reason": "repo_not_found"})
            continue
        if st != 200 or "default_branch" not in meta:
            rows.append({"repo": repo, "branch": "-", "expected": expected,
                         "reporter": ("unknown", f"repo read HTTP {st}"),
                         "requirement": ("-", "-"), "verdict": "UNKNOWN", "reason": "repo_read_failed"})
            continue
        branch = meta["default_branch"]
        rep = check_reporter(org, repo, branch, expected)
        req = check_requirement(org, repo, branch, expected, known)
        verdict, reason = combine(rep[0], req[0])
        rows.append({"repo": repo, "branch": branch, "expected": expected,
                     "reporter": rep, "requirement": req, "verdict": verdict, "reason": reason})
    return rows


def render(run_ts, rows, scope_note, not_enrolled):
    drift = [r for r in rows if r["verdict"] == "DRIFT"]
    unknown = [r for r in rows if r["verdict"] == "UNKNOWN"]
    status = "DRIFT" if drift else "UNKNOWN" if unknown else "CLEAN"
    L = []
    L.append(f"### Auth-coverage drift detector — run {run_ts}")
    L.append("")
    L.append(f"**status: {status}** · gated repos: {len(rows)} · "
             f"OK: {sum(1 for r in rows if r['verdict'] == 'OK')} · "
             f"DRIFT: {len(drift)} · UNKNOWN: {len(unknown)}")
    L.append("")
    L.append(f"_Scope: {scope_note}_")
    L.append("")
    L.append("Read-only across the org; the only write is this tracking issue. "
             "A repo is OK only if a reporter for its expected context exists (a), "
             "the effective branch rules require that context (b), and reported == "
             "required (c). Anything unverifiable is UNKNOWN, never OK.")
    L.append("")
    L.append("| Repo | Branch | Expected context | (a) reporter | (b)/(c) requirement | Verdict |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in sorted(rows, key=lambda x: (x["verdict"] != "DRIFT", x["verdict"] != "UNKNOWN", x["repo"])):
        rep = f"{r['reporter'][0]}: {r['reporter'][1]}"
        req = f"{r['requirement'][0]}: {r['requirement'][1]}"
        L.append(f"| {r['repo']} | `{r['branch']}` | `{r['expected']}` | {rep} | {req} | "
                 f"**{r['verdict']}** ({r['reason']}) |")
    L.append("")
    if not_enrolled:
        L.append("#### Not enrolled (documented, not checked)")
        for n in not_enrolled:
            L.append(f"- **{n['repo']}** — {n['reason']}")
        L.append("")
    L.append(f"`watchman: alive. run completed {run_ts}.`")
    return status, "\n".join(L)


def tracking_issue(org, report):
    st, issues = gh("GET", f"/repos/{org}/{report['repo']}/issues",
                    params={"labels": report["label"], "state": "open", "per_page": 100})
    if st == 200 and isinstance(issues, list):
        for i in issues:
            if "pull_request" not in i:
                return i["number"]
    return None


def selftest():
    wf = {"jobs": {"auth-coverage": {"runs-on": "x"},
                   "named": {"name": "auth-guard", "runs-on": "x"},
                   "reusable": {"uses": "org/.github/.github/workflows/x.yml@sha"}}}
    got = reported_contexts(wf)
    assert "auth-coverage" in got, got
    assert "auth-guard" in got, got
    assert "reusable" not in got and "x.yml" not in " ".join(got), got
    assert reported_contexts({}) == [] and reported_contexts({"jobs": None}) == []
    assert combine("ok", "ok") == ("OK", "ok")
    assert combine("missing", "ok")[0] == "DRIFT"
    assert combine("ok", "missing")[0] == "DRIFT"
    assert combine("ok", "mismatch") == ("DRIFT", "context_mismatch")
    assert combine("unknown", "ok")[0] == "UNKNOWN"
    assert combine("ok", "unknown")[0] == "UNKNOWN"
    # unknown wins over a missing reporter -- never downgrade unverifiable to drift-or-ok
    assert combine("missing", "unknown")[0] == "UNKNOWN"
    print("selftest: OK")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(DATA_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    org, report = cfg["org"], cfg["report"]
    known = sorted({g["expected_context"] for g in cfg["gated_repos"]})
    scope_note = probe_org_scope(org, known)
    rows = evaluate(cfg)
    status, body = render(run_ts, rows, scope_note, cfg.get("not_enrolled", []))
    print(body)

    num = tracking_issue(org, report)
    if status == "CLEAN":
        if num:
            gh("POST", f"/repos/{org}/{report['repo']}/issues/{num}/comments",
               body={"body": f"Auto-resolved: all gated repos green as of {run_ts}.\n\n{body}"},
               token=WRITE_TOKEN)
            gh("PATCH", f"/repos/{org}/{report['repo']}/issues/{num}",
               body={"state": "closed"}, token=WRITE_TOKEN)
        print(f"CLEAN — {len(rows)} gated repos green.")
        return 0
    if num:
        st, res = gh("POST", f"/repos/{org}/{report['repo']}/issues/{num}/comments",
                     body={"body": body}, token=WRITE_TOKEN)
    else:
        st, res = gh("POST", f"/repos/{org}/{report['repo']}/issues",
                     body={"title": report["issue_title"], "labels": [report["label"]], "body": body},
                     token=WRITE_TOKEN)
    if st not in (200, 201):
        print(f"::error::failed to write tracking issue: HTTP {st} {res}", file=sys.stderr)
        return 1
    print(f"::error::auth-coverage {status} — see {res.get('html_url')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
