#!/usr/bin/env python3
"""
Detect required status checks that NOTHING on the repo can satisfy.

WHY THIS EXISTS, and why a written rule was not enough:
  CI-G14 already says a context may be required on a repo only after it has
  reported there. Twice now a required context has been added to a repo that
  could not satisfy it, blocking every PR with no red X. Both times the rule
  existed and both times the person who added the context was not the person
  who had read the rule.

  This org CANNOT attribute the change after the fact: repository rulesets
  expose no creator field, and the audit-log API is Enterprise-only (404 on
  Team). So prevention is unavailable and attribution is unavailable. The only
  remaining control is DETECTION, which is what this is.

WHAT IT FLAGS
  For every active repo, every context required on the default branch that has
  NOT been observed reporting there recently.

⚠ LIMITS, STATED SO NOBODY TREATS THIS AS PROOF
  * A quiet repo with no recent runs looks identical to a repo with a dead
    gate. Those are reported as UNKNOWN, never as BROKEN.
  * Check-run history is purged at the Actions retention setting (default 90d
    from 2026-10-01), so absence of history is not absence of a reporter.
  * A sample of pull requests sorted by `updated` is NOT a recency sample — a
    bulk label or branch-delete pass re-dates old PRs to the top. This script
    reads workflow RUNS by created-date instead, which does not have that flaw.
"""
import json, os, sys, urllib.request, urllib.error, collections

TOK = os.environ["GITHUB_TOKEN"]
ORG = os.environ.get("ORG", "purpusgit")

def api(path):
    rq = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {TOK}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "required-context-reporter-audit"})
    try:
        return json.load(urllib.request.urlopen(rq, timeout=30))
    except urllib.error.HTTPError as e:
        return {"__status": e.code}
    except Exception as e:
        return {"__error": str(e)}

def required_contexts(repo, branch):
    rules = api(f"/repos/{ORG}/{repo}/rules/branches/{branch}")
    out = []
    if isinstance(rules, list):
        for r in rules:
            if r.get("type") == "required_status_checks":
                for c in r["parameters"].get("required_status_checks", []):
                    out.append((c["context"], r.get("ruleset_id")))
    bp = api(f"/repos/{ORG}/{repo}/branches/{branch}/protection")
    sc = (bp or {}).get("required_status_checks") or {}
    for c in (sc.get("contexts") or [x.get("context") for x in sc.get("checks", [])]):
        if c:
            out.append((c, "classic-branch-protection"))
    return out

def observed_contexts(repo):
    """Names seen on recent completed check runs. Runs are read by created-date."""
    seen, runs_seen = set(), 0
    runs = api(f"/repos/{ORG}/{repo}/actions/runs?per_page=30")
    for run in (runs.get("workflow_runs") or []):
        sha = run.get("head_sha")
        if not sha:
            continue
        runs_seen += 1
        cr = api(f"/repos/{ORG}/{repo}/commits/{sha}/check-runs")
        for c in (cr.get("check_runs") or []):
            seen.add(c["name"])
        if len(seen) > 60:
            break
    return seen, runs_seen

def main():
    repos, page = [], 1
    while page <= 3:
        b = api(f"/orgs/{ORG}/repos?per_page=100&page={page}&type=all")
        if not isinstance(b, list) or not b:
            break
        repos += b
        if len(b) < 100:
            break
        page += 1

    broken, unknown, ok = [], [], 0
    for r in sorted([x for x in repos if not x["archived"]], key=lambda x: x["name"]):
        repo, branch = r["name"], r["default_branch"]
        req = required_contexts(repo, branch)
        if not req:
            continue
        seen, runs_seen = observed_contexts(repo)
        for ctx, src in req:
            if ctx in seen:
                ok += 1
            elif runs_seen == 0:
                unknown.append((repo, branch, ctx, src, "no recent runs"))
            else:
                broken.append((repo, branch, ctx, src, f"{runs_seen} runs scanned"))

    lines = ["# Required contexts with no observed reporter", ""]
    if broken:
        lines += ["## ⛔ REQUIRED BUT NEVER OBSERVED", "",
                  "Every pull request in these repos may be blocked on an **absence**, "
                  "which presents as a PR that never becomes mergeable with no red X.", "",
                  "| repo | branch | required context | source | evidence |",
                  "|---|---|---|---|---|"]
        for row in broken:
            lines.append("| `{}` | `{}` | `{}` | `{}` | {} |".format(*row))
    else:
        lines += ["## ✅ No required context is missing a reporter.", ""]
    if unknown:
        lines += ["", "## ℹ UNKNOWN — repo too quiet to judge", "",
                  "No recent workflow runs, so absence of evidence is not evidence of absence.", "",
                  "| repo | branch | required context | source |", "|---|---|---|---|"]
        for repo, branch, ctx, src, _ in unknown:
            lines.append(f"| `{repo}` | `{branch}` | `{ctx}` | `{src}` |")
    lines += ["", f"_Contexts verified reporting: {ok}_"]

    report = "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    # Never fail the schedule on a finding -- this reports, it does not gate.
    return 0

if __name__ == "__main__":
    sys.exit(main())
