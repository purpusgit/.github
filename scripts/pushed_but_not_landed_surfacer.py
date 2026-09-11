#!/usr/bin/env python3
"""Pushed-but-not-landed surfacer.

Reuses the design lane #12's drift-detector spec ruled on (lanes#12,
comment 5624212485 "Drift-detector specification v1", ruled 5630167286):
distinct status strings for "nothing found" vs "found nothing because I
stopped watching" are never collapsed, every run reports (never silent-only-
on-findings), and the report says plainly what it does NOT cover.

This is a DIFFERENT question from lane #12's own detector (which watches
`push` to trunk for security-sensitive file changes on 5 named repos).
This one answers the question named in the git-gates handoff (lanes#59,
comment 5630054609): "pushed a commit, no PR" and "PR raised, not merged"
must never again silently read as "done" -- across every active repo in
the org, not a named 5.

Two independent findings, both read-only, both schedule-driven (no push
trigger -- there is no push EVENT that signals "still no PR"; that is an
absence, only detectable by polling):

  BRANCH_AHEAD_NO_PR  -- a branch has commits the default branch lacks,
                         and no open PR has that branch as its head.
  AGED_OPEN_PR        -- an open PR has sat past AGE_THRESHOLD_DAYS since
                         creation.

NOT covered, stated once rather than per-row (same discipline as lane #12's
own spec, section "What it deliberately does not cover"): this reads
GitHub's API only. It has no host access and cannot tell you whether an
unlanded branch was ever deployed by hand, or whether an aged PR is aged
because it is blocked on a human, not on process. It answers "is there an
open loop GitHub can see," not "does the open loop matter."

Never blocking: read-only API calls, comment-only output, no status check,
no branch-protection write. This is Job 3, LANE-2 (git-gates, lanes#2/#59).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ORG = "purpusgit"
REPORT_REPO = "lanes"
REPORT_ISSUE = 2  # git-gates' own mailbox -- same convention lane #12 used
AGE_THRESHOLD_DAYS = 7
# Org-wide delete-branch-on-merge armed 2026-09-06 (lanes#59, comment 5562242566).
# A branch last touched before that date is as likely a pre-cutover squash-merge
# leftover (compare still shows ahead_by>0 forever -- squash never makes the
# individual commits present in default) as it is a genuinely unlanded push.
# These are reported as a COUNT, never dropped silently, but kept out of the
# full-detail table so 300+ pre-cutover leftovers don't bury the actionable rows.
BRANCH_CUTOVER_DATE = "2026-09-06T00:00:00Z"
API = "https://api.github.com"
# Some repos in this org carry hundreds of stale branches predating org-wide
# delete-branch-on-merge (armed 2026-09-06). A serial `compare` call per
# branch does not finish in reasonable time or Actions minutes at that scale
# (pkg_orbit_japa alone: 362 branches). Parallelised per repo; GitHub's REST
# rate limit for a GitHub App installation token is 5000 req/hr, and this is
# a nightly job, not a per-PR gate -- there is room for this without a
# fancier (GraphQL-batched) rewrite.
BRANCH_COMPARE_WORKERS = 10
REPO_SCAN_WORKERS = 4

TOKEN = os.environ["GH_TOKEN"]
# Posting the report comment uses a SEPARATE, narrowly-scoped write token
# (issues:write on purpusgit/lanes only) -- the workflow mints it distinct
# from the org-wide read token above on purpose (least privilege: the read
# token that touches every repo's branches/PRs has no write scope at all).
# Falls back to TOKEN for local/manual runs where only one token exists.
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
            # Transient network failure (sandbox egress blip, connection pool
            # exhaustion under BRANCH_COMPARE_WORKERS concurrency) -- retry with
            # backoff rather than crashing the whole run over one flaky call.
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))
    return 0, {"message": f"transient network failure after {_retries} attempts: {last_exc!r}"}


class GhListError(Exception):
    """A paginated GitHub read got a non-200. Distinct from "empty page, done" --
    collapsing the two (the original bug) let a 403/5xx render as CLEAN."""


def gh_paginated(path, params=None):
    params = dict(params or {})
    params["per_page"] = 100
    page = 1
    out = []
    while True:
        params["page"] = page
        status, batch = gh("GET", path, params)
        if status == 200 and isinstance(batch, list):
            if not batch:
                break  # legitimate end of pagination
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            continue
        raise GhListError(f"GET {path} page {page} -> HTTP {status}: {batch}")
    return out


def list_active_repos():
    repos = gh_paginated(f"/orgs/{ORG}/repos", {"type": "all"})
    return [r for r in repos if not r.get("archived") and not r.get("disabled")]


def age_days(iso_ts):
    dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _compare_one(repo_name, default_branch, branch_name):
    status, cmp = gh("GET", f"/repos/{ORG}/{repo_name}/compare/{default_branch}...{branch_name}")
    return branch_name, status, cmp


def scan_repo(repo_name, default_branch):
    findings = {"branch_ahead_no_pr": [], "aged_open_pr": [], "stale_pre_cutover": 0}
    errors = []

    branches = gh_paginated(f"/repos/{ORG}/{repo_name}/branches")
    open_prs = gh_paginated(f"/repos/{ORG}/{repo_name}/pulls", {"state": "open"})
    open_pr_heads = {pr["head"]["ref"] for pr in open_prs}

    candidates = [b["name"] for b in branches
                  if b["name"] != default_branch and b["name"] not in open_pr_heads]
    # Branches with an open PR need no compare call at all -- they are not
    # "no PR" by definition, whatever their ahead_by is.

    with ThreadPoolExecutor(max_workers=BRANCH_COMPARE_WORKERS) as pool:
        futures = [pool.submit(_compare_one, repo_name, default_branch, name)
                   for name in candidates]
        for fut in as_completed(futures):
            name, status, cmp = fut.result()
            if status != 200:
                errors.append(f"compare {default_branch}...{name} -> HTTP {status}")
                continue
            ahead_by = cmp.get("ahead_by", 0)
            if ahead_by <= 0:
                continue
            commits = cmp.get("commits", [])
            last = commits[-1] if commits else None
            last_date = (last or {}).get("commit", {}).get("author", {}).get("date", "unknown")
            if last_date != "unknown" and last_date < BRANCH_CUTOVER_DATE:
                # Pre-cutover leftover: as likely a squash-merge already landed
                # (compare never zeroes out for those) as a real unlanded push.
                # Counted, never dropped silently -- just kept out of the
                # detail table so it can't bury the actionable rows.
                findings["stale_pre_cutover"] += 1
                continue
            findings["branch_ahead_no_pr"].append({
                "branch": name,
                "ahead_by": ahead_by,
                "last_commit_sha": (last or {}).get("sha", "")[:10],
                "last_commit_date": last_date,
                "last_commit_author": (last or {}).get("commit", {}).get("author", {}).get("name", "unknown"),
            })

    for pr in open_prs:
        days = age_days(pr["created_at"])
        if days > AGE_THRESHOLD_DAYS:
            findings["aged_open_pr"].append({
                "number": pr["number"],
                "title": pr["title"],
                "age_days": round(days, 1),
                "draft": pr.get("draft", False),
                "mergeable_state": pr.get("mergeable_state", "unknown"),
            })

    return findings, errors


MAX_ROWS_PER_TABLE = 50


def render_report(run_started, per_repo, scan_errors, total_repos):
    total_branch = sum(len(f["branch_ahead_no_pr"]) for f in per_repo.values())
    total_aged = sum(len(f["aged_open_pr"]) for f in per_repo.values())
    total_stale = sum(f.get("stale_pre_cutover", 0) for f in per_repo.values())
    if total_branch or total_aged:
        status = "FINDINGS"
    elif scan_errors:
        # Every repo failing to scan must never read the same as "scanned,
        # found nothing" -- that is the exact silent collapse this surfacer
        # exists to prevent (see module docstring).
        status = "SCAN_ERROR"
    else:
        status = "CLEAN"

    lines = []
    lines.append(f"### Pushed-but-not-landed surfacer — run {run_started}")
    lines.append("")
    lines.append(f"**status: {status}** · repos scanned: {total_repos} · "
                  f"branches-ahead-no-PR: {total_branch} · aged open PRs (>{AGE_THRESHOLD_DAYS}d): {total_aged} · "
                  f"pre-cutover stale branches (counted, not detailed): {total_stale}")
    lines.append("")
    lines.append("Read-only, comment-only. No status check, no branch-protection write, "
                  "nothing blocked. GitHub API only — does not know whether an unlanded "
                  "branch was ever hand-deployed, or why an aged PR is stuck. Branches last "
                  f"touched before {BRANCH_CUTOVER_DATE} (org-wide delete-branch-on-merge cutover) "
                  "are counted above but excluded from the detail table below — a squash-merged "
                  "branch from before that date can show ahead_by>0 forever with no real gap.")
    lines.append("")

    if total_branch:
        all_rows = [(repo, row) for repo, f in per_repo.items() for row in f["branch_ahead_no_pr"]]
        # Most-recently-pushed first: a branch that moved yesterday is more
        # actionable than one that hasn't moved since 2025. Sorting does not
        # change what's counted in the header total above -- only what's shown.
        # "unknown" dates sort last, never first (a bare string compare put them
        # first under reverse=True -- 'u' > digit characters).
        all_rows.sort(key=lambda rb: rb[1]["last_commit_date"] if rb[1]["last_commit_date"] != "unknown" else "",
                      reverse=True)
        shown = all_rows[:MAX_ROWS_PER_TABLE]
        lines.append("#### Branches ahead of default with no open PR"
                      + (f" (showing {len(shown)} most-recent of {len(all_rows)})"
                         if len(all_rows) > len(shown) else ""))
        lines.append("")
        lines.append("| Repo | Branch | Ahead by | Last commit | Author | Date |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for repo, row in shown:
            lines.append(f"| {repo} | `{row['branch']}` | {row['ahead_by']} | "
                          f"`{row['last_commit_sha']}` | {row['last_commit_author']} | "
                          f"{row['last_commit_date']} |")
        lines.append("")

    if total_aged:
        all_rows = [(repo, row) for repo, f in per_repo.items() for row in f["aged_open_pr"]]
        all_rows.sort(key=lambda rb: rb[1]["age_days"], reverse=True)
        shown = all_rows[:MAX_ROWS_PER_TABLE]
        lines.append(f"#### Open PRs older than {AGE_THRESHOLD_DAYS} days"
                      + (f" (showing {len(shown)} oldest of {len(all_rows)})"
                         if len(all_rows) > len(shown) else ""))
        lines.append("")
        lines.append("| Repo | PR | Age (days) | Draft | Mergeable state |")
        lines.append("| --- | --- | --- | --- | --- |")
        for repo, row in shown:
            lines.append(f"| {repo} | #{row['number']} {row['title']} | {row['age_days']} | "
                          f"{row['draft']} | {row['mergeable_state']} |")
        lines.append("")

    if scan_errors:
        lines.append("#### Scan errors (repo skipped or partial)")
        lines.append("")
        for e in scan_errors:
            lines.append(f"- {e}")
        lines.append("")

    lines.append(f"`watchman: alive. run completed {run_started}.`")
    return "\n".join(lines)


def _scan_one_repo(r):
    name = r["name"]
    default_branch = r["default_branch"]
    try:
        return name, scan_repo(name, default_branch), None
    except Exception as exc:  # never let one bad repo kill the whole run
        # includes GhListError from the branches/open-PRs listing calls -- a
        # repo whose listing 403'd/5xx'd lands here as a named scan error,
        # never as a silent "this repo had nothing" omission.
        empty = {"branch_ahead_no_pr": [], "aged_open_pr": [], "stale_pre_cutover": 0}
        return name, (empty, []), repr(exc)


def _error_report(run_started, exc):
    return (
        f"### Pushed-but-not-landed surfacer — run {run_started}\n\n"
        f"**status: ERROR** · could not list org repos: {exc}\n\n"
        "No scan was performed this run. This is NOT a clean result -- treat "
        "it as \"stopped watching\", not \"nothing found\", and investigate the "
        "read token / GitHub API before the next scheduled run.\n\n"
        f"`watchman: alive but failed. run completed {run_started}.`"
    )


def main():
    run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        repos = list_active_repos()
    except GhListError as exc:
        # The one call with no per-repo isolation to fall back on. Must never
        # collapse into a "CLEAN, 0 repos scanned" report -- that is exactly
        # the silent-stopped-watching failure this surfacer exists to prevent.
        body = _error_report(run_started, exc)
        gh("POST", f"/repos/{ORG}/{REPORT_REPO}/issues/{REPORT_ISSUE}/comments",
           body={"body": body}, token=WRITE_TOKEN)
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)

    per_repo = {}
    scan_errors = []
    with ThreadPoolExecutor(max_workers=REPO_SCAN_WORKERS) as pool:
        futures = [pool.submit(_scan_one_repo, r) for r in repos]
        for fut in as_completed(futures):
            name, (findings, errs), exc = fut.result()
            if exc:
                scan_errors.append(f"{name}: unhandled exception {exc}")
                continue
            for e in errs:
                scan_errors.append(f"{name}: {e}")
            if findings["branch_ahead_no_pr"] or findings["aged_open_pr"] or findings["stale_pre_cutover"]:
                per_repo[name] = findings

    per_repo = dict(sorted(per_repo.items()))
    scan_errors.sort()
    body = render_report(run_started, per_repo, scan_errors, len(repos))
    status, res = gh("POST", f"/repos/{ORG}/{REPORT_REPO}/issues/{REPORT_ISSUE}/comments",
                      body={"body": body}, token=WRITE_TOKEN)
    if status not in (200, 201):
        print(f"::error::failed to post report comment: HTTP {status} {res}", file=sys.stderr)
        sys.exit(1)
    print(f"Posted report: {res.get('html_url')}")
    print(body)
    if repos and len(scan_errors) >= len(repos):
        # Every repo failed to scan (e.g. token rate-limited/revoked mid-run).
        # The report is posted either way (never silent) but the job must
        # still go red -- 0% actually scanned is not a green run.
        print("::error::every repo failed to scan this run -- see Scan errors section above", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
