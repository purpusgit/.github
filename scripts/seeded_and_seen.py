#!/usr/bin/env python3
"""seeded-and-seen gate: a UI PR says how it was tested, with pictures (founder rule SEEDED-AND-SEEN-1, JAPA-01, specs#411).

It needs NO checkout of PR code. It reads the PR body, labels and draft flag from the event payload ($GITHUB_EVENT_PATH),
the changed files from the API, and (only for repo paths in the evidence) the contents API. Stdlib only.

FIRES when the PR is not a draft and changes (not only deletes) a file under lib/screens/ or lib/widgets/, or any lib/**
file named *_screen.dart, *_page.dart or *_widget.dart (the org also writes the dot form, *.screen.dart). A draft, or a PR
with no such file, passes with one "not applicable" line.

REQUIRES a `## TESTED` section (`###` also accepted) with:
  (a) one or more named test accounts matching TEST_ACCOUNT_PATTERN (default JAPA_TEST_[A-Z0-9_]+), and nothing that looks
      like a password or token next to them (a belt to the existing secret scan);
  (b) a `Build:` line naming sandbox or dev, and a `Seeded:` line with ids;
  (c) a `States:` list, one list item per state;
  (d) at least one image per state: a markdown image, an <img src>, or a repo path to an image that exists in the PR head
      (or on the branch named by an `Evidence: <branch>` line), checked with the contents API.
An external image URL is accepted as written: it cannot be fetched with the job's token.

EXEMPT: the label `tested-n/a` AND a `tested-n/a: <reason>` line of at least 10 characters. LIMIT, said plainly: every
session posts as the same GitHub account, so "a reviewer approved the exemption" cannot be checked by a machine. This gate
checks the label and the reason are present and says so in the step summary; the human reviewer must judge the reason.

FAILS CLOSED: a missing body, an unreadable event, a bad pattern or any API error is red, with one line saying what.
Exit 0 pass or not applicable, 1 the PR fails a requirement, 2 the gate could not run.

shortcut: the secret check is a heuristic (keyword next to the account, a JWT shape, a long mixed token on the account's
          line); it will miss a password written as a plain word. Upgrade by sharing the secret scanner's patterns.
Env: TEST_ACCOUNT_PATTERN, GH_TOKEN, GITHUB_EVENT_PATH, GITHUB_STEP_SUMMARY. Test: python3 scripts/test_seeded_and_seen.py
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PATTERN = r"JAPA_TEST_[A-Z0-9_]+"
EXEMPT_LABEL = "tested-n/a"
SCREEN_FILE = re.compile(r"^lib/.*[._](?:screen|page|widget)\.dart$")
IMG_EXT = r"(?:png|jpe?g|gif|webp|svg|avif)"
MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
REPO_PATH = re.compile(r"(?<![\w/.:@-])((?:[\w.-]+/)+[\w.-]+\.%s)(?![\w/])" % IMG_EXT, re.I)
SECRET_WORD = re.compile(r"\b(?:password|passwd|pwd|pass|token|secret|otp|pin|api[_-]?key|bearer)\b\s*(?:[:=]|is\b)\s*\S{3,}", re.I)
SECRET_WORD_NEAR = re.compile(r"\b(?:password|passwd|pwd|token|secret|api[_-]?key|bearer)\b\s*[:=]\s*\S{3,}", re.I)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.")
LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{24,}(?![A-Za-z0-9+/_=-])")
PLACEHOLDER = re.compile(r"^[\s<>\[\]()*_`.-]*(?:todo|tbd|none|n/?a|na|ids?|xxx*|\.\.\.)?[\s<>\[\]()*_`.-]*$", re.I)


class GateError(Exception):
    """The gate could not run (exit 2)."""


class ApiError(GateError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def fires(files):
    """True if the changed files make this a UI PR. files: [{'filename', 'status'}]; a pure deletion has nothing to see."""
    for f in files:
        name, status = f.get("filename", ""), f.get("status", "")
        if status == "removed":
            continue
        if name.startswith(("lib/screens/", "lib/widgets/")) or SCREEN_FILE.match(name):
            return True
    return False


def tested_section(body):
    m = re.search(r"^(#{2,3})[ \t]+TESTED[ \t]*:?[ \t]*$", body, re.I | re.M)
    if not m:
        return None
    rest = body[m.end():]
    nxt = re.search(r"^#{1,%d}[ \t]+\S" % len(m.group(1)), rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def label_value(section, key):
    """The text after `Key:` on a line (optionally a list item or bold), or None."""
    m = re.search(r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?%s(?:\*\*)?[ \t]*:(?:\*\*)?[ \t]*(.*)$" % key, section, re.I | re.M)
    return m.group(1).strip() if m else None


def mask_noise(line, accounts):
    """The line with images, URLs and account names blanked, so only a credential-looking residue is left."""
    line = MD_IMAGE.sub(" ", line)
    line = re.sub(r"https?://\S+", " ", line)
    return accounts.sub(" ACCT ", line)


def account_problems(section, pattern):
    accounts = re.compile(r"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])" % pattern)
    lines = section.splitlines()
    problems, found = [], False
    for i, ln in enumerate(lines):
        if not accounts.search(ln):
            continue
        found = True
        here = mask_noise(ln, accounts)
        near = " ".join(mask_noise(lines[j], accounts) for j in (i - 1, i + 1) if 0 <= j < len(lines))
        mixed = [t for t in LONG_TOKEN.findall(here) if re.search(r"[A-Za-z]", t) and re.search(r"\d", t)]
        if SECRET_WORD.search(here) or JWT.search(here) or mixed or SECRET_WORD_NEAR.search(near):
            problems.append("a password or token appears next to test account %s; name the account only, never its secret" % accounts.search(ln).group(0))
    if not found:
        problems.append("no named test account matching %s; list one or more (for example JAPA_TEST_SEEDED_A)" % pattern)
    return problems


def state_items(section):
    """The list items under a `States:` line or `### States` heading, each as its full text (item plus continuation)."""
    lines = section.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^[ \t]*(?:#{3,4}[ \t]+|[-*+][ \t]+)?(?:\*\*)?States(?:\*\*)?[ \t]*:?(?:\*\*)?[ \t]*$", ln, re.I):
            start = i + 1
            break
    if start is None:
        return None
    items, cur, base = [], None, None
    for ln in lines[start:]:
        m = re.match(r"^([ \t]*)(?:[-*+]|\d+[.)])[ \t]+(.*)$", ln)
        indent = len(ln) - len(ln.lstrip())
        if m and (base is None or indent <= base):
            base = indent if base is None else base
            cur = [m.group(2)]
            items.append(cur)
        elif cur is not None and (not ln.strip() or indent > (base or 0)):
            cur.append(ln)
        elif ln.strip():
            break
    return ["\n".join(x) for x in items]


def images_in(text):
    """(urls, repo_paths) found in a state's text."""
    urls = [u for u in MD_IMAGE.findall(text) + HTML_IMAGE.findall(text)]
    external = [u for u in urls if re.match(r"^https?://", u, re.I)]
    local = [u for u in urls if not re.match(r"^(?:https?:|data:|//)", u, re.I)]
    stripped = MD_IMAGE.sub(" ", HTML_IMAGE.sub(" ", re.sub(r"https?://\S+", " ", text)))
    paths = [p for p in REPO_PATH.findall(stripped)] + [u.lstrip("./") for u in local]
    return external, paths


def evidence_ref(section, head_sha):
    ev = label_value(section, r"Evidence(?:[ \t]+branch)?")
    if ev:
        branch = re.sub(r"[`*<>\s]", "", ev.split()[0] if ev.split() else "")
        if branch and not PLACEHOLDER.match(branch):
            return branch
    return head_sha


def check_exemption(body, labels):
    """(applies, problems). Both the label and a reason of 10+ characters are needed."""
    has_label = EXEMPT_LABEL in labels
    m = re.search(r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?tested-n/a(?:\*\*)?[ \t]*:(?:\*\*)?[ \t]*(.*)$", body or "", re.I | re.M)
    reason = m.group(1).strip() if m else None
    reason_ok = bool(reason) and len(re.sub(r"[<>`*_]", "", reason).strip()) >= 10 and not re.match(r"^<[^>]*>$", reason)
    if has_label and reason_ok:
        return True, []
    if has_label:
        return False, ["label `tested-n/a` is set but the body has no `tested-n/a: <reason>` line of at least 10 characters"]
    if reason is not None:
        return False, ["the body has a `tested-n/a:` line but the label `tested-n/a` is not set"]
    return False, []


def evaluate(pr, files, pattern, repo_path_exists):
    """-> (code, lines). pr: {'body','draft','labels':[names],'head_sha'}. repo_path_exists(path, ref) -> bool may raise ApiError."""
    if pr.get("draft"):
        return 0, ["seeded-and-seen: not applicable (the PR is a draft)."]
    if not fires(files):
        return 0, ["seeded-and-seen: not applicable (no screen or widget file changed)."]
    body, labels = pr.get("body") or "", set(pr.get("labels") or [])
    exempt, ex_problems = check_exemption(body, labels)
    if exempt:
        return 0, ["seeded-and-seen: exempt by label `tested-n/a` with a stated reason. Not machine-checked: every session posts as the same "
                   "account, so only a human reviewer can judge that reason."]
    if not body.strip():
        return 1, ["the PR has no body; a UI PR needs a `## TESTED` section (see SEEDED-AND-SEEN-1)"] + ex_problems
    section = tested_section(body)
    if section is None:
        return 1, ["no `## TESTED` section in the PR body"] + ex_problems
    problems = list(ex_problems)
    problems += account_problems(section, pattern)
    build, seeded = label_value(section, "Build"), label_value(section, "Seeded")
    if not build or not re.search(r"\b(?:sandbox|dev)\b", build, re.I):
        problems.append("no `Build:` line naming sandbox or dev")
    if not seeded or PLACEHOLDER.match(seeded):
        problems.append("no `Seeded:` line with the seeded ids")
    states = state_items(section)
    if not states:
        problems.append("no `States:` list; list each state tested as a list item, each with an image")
    else:
        ref = evidence_ref(section, pr.get("head_sha") or "")
        for n, text in enumerate(states, 1):
            label = re.sub(r"\s+", " ", text.splitlines()[0]).strip()[:60]
            external, paths = images_in(text)
            if not external and not paths:
                problems.append("state %d (%s) has no image; add a markdown image, an <img src>, or a repo path to an image" % (n, label))
                continue
            if external:
                continue
            missing = [p for p in paths if not repo_path_exists(p, ref)]
            if len(missing) == len(paths):
                problems.append("state %d (%s): image path %s does not exist at %s" % (n, label, missing[0], ref[:12]))
    return (1 if problems else 0), (problems or ["seeded-and-seen passed: accounts, build, seeded ids, and an image for each state."])


# ── the I/O around it ──────────────────────────────────────────────────────────────────────────────────────────────
def http_get(path, token, tries=3):
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, None
            last = ApiError(e.code, "GitHub API %s returned %d" % (path, e.code))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = ApiError(0, "GitHub API %s failed: %s" % (path, e))
        time.sleep(1 + k)
    raise last


def changed_files(repo, number, get):
    out, page = [], 1
    while page <= 30:
        status, data = get("/repos/%s/pulls/%d/files?per_page=100&page=%d" % (repo, number, page))
        if status != 200 or not isinstance(data, list):
            raise ApiError(status, "could not list the PR's changed files (HTTP %s)" % status)
        out += [{"filename": f.get("filename", ""), "status": f.get("status", "")} for f in data]
        if len(data) < 100:
            return out
        page += 1
    raise ApiError(0, "the PR changes more than 3000 files; cannot list them all")


def make_exists(repo, get):
    def exists(path, ref):
        status, _ = get("/repos/%s/contents/%s?ref=%s" % (repo, urllib.parse.quote(path), urllib.parse.quote(ref, safe="")))
        if status == 200:
            return True
        if status == 404:
            return False
        raise ApiError(status, "could not check %s at %s (HTTP %s)" % (path, ref[:12], status))
    return exists


def run(event, pattern, get):
    pr_event = (event or {}).get("pull_request")
    if not pr_event:
        return 0, ["seeded-and-seen: not applicable (the event has no pull_request)."]
    repo = (event.get("repository") or {}).get("full_name")
    number = pr_event.get("number")
    if not repo or not number:
        raise GateError("the event payload has no repository or pull request number")
    try:
        re.compile(pattern)
    except re.error as e:
        raise GateError("TEST_ACCOUNT_PATTERN is not a valid regular expression: %s" % e)
    pr = {"body": pr_event.get("body"), "draft": bool(pr_event.get("draft")),
          "labels": [l.get("name") for l in pr_event.get("labels") or []], "head_sha": (pr_event.get("head") or {}).get("sha", "")}
    if pr["draft"]:
        return evaluate(pr, [], pattern, None)
    return evaluate(pr, changed_files(repo, number, get), pattern, make_exists(repo, get))


def main():
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
            event = json.load(fh)
    except (KeyError, OSError, json.JSONDecodeError) as e:
        print("::error::seeded-and-seen could not read the event payload (%s). This is a gate failure, not a pass." % e)
        return 2
    token = os.environ.get("GH_TOKEN", "")
    pattern = os.environ.get("TEST_ACCOUNT_PATTERN") or DEFAULT_PATTERN
    try:
        code, lines = run(event, pattern, lambda p: http_get(p, token))
    except GateError as e:
        print("::error::seeded-and-seen could not run: %s. This is a gate failure, not a pass." % e)
        return 2
    for ln in lines:
        print(("::error::" if code else "") + ln)
    if code:
        print("\nFix: add a `## TESTED` section to the PR body with `Accounts:` (named test accounts), `Build:` (sandbox or dev), "
              "`Seeded:` (ids) and a `States:` list where each state carries an image. Exempt: label `tested-n/a` plus a "
              "`tested-n/a: <reason>` line (a human reviewer judges the reason; this gate cannot check an approval).")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("### seeded-and-seen\n\n" + "\n".join("- " + ln for ln in lines) + "\n\n"
                         "_The `tested-n/a` exemption is honoured on the label and a stated reason only. Every session posts as the "
                         "same account, so a reviewer's approval of it cannot be machine-checked; a human must judge the reason._\n")
        except OSError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
