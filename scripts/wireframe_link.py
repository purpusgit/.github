#!/usr/bin/env python3
"""ux-wireframe-link (CALM-1 spec rev 5.1, section 3.3): no new look without approved frames. Run as `python3 -I scripts/wireframe_link.py`.

No checkout of the PR's repo: the PR's files, the BASE manifest and the ledger come from the GitHub contents API.
A PR that changes a manifest screen's file needs, in its description, one of:
  `Wireframe: <link>`            a purpusgit/specs main link to a frames file listed in flutter-ui/approvals.tsv, whose ONE row
                                 lists every changed screen. A `delegated` row never covers a flavour home (`flavour_home: true`)
                                 or a flavour whose `new_until` is still in the future: those need a `founder` row.
  `No visual change: <why>`      accepted for now; the pixel compare belongs to the web-width harness piece ("pending").
Judged by the BASE manifest's fields, so a PR cannot unset flavour_home to dodge. Row file decides the trigger.

Ledger (tab separated, `#` comments and a header line starting `frames` skipped):
  frames_file <TAB> screens (row names or repo:Class, comma separated) <TAB> kind (founder|delegated) <TAB> date <TAB> link
Env: GITHUB_TOKEN (this repo's API), SPECS_READ_TOKEN (optional: the specs ledger), REPO, PR_NUMBER, BASE_REF (branch name), PR_BODY,
CALM_MODE (warn default | enforce), GITHUB_STEP_SUMMARY. Exit: warn 0 (findings printed), enforce 1 on findings; a diff or base manifest
that cannot be read exits 2; an unreadable ledger is a notice + pass in warn and exit 2 in enforce.
"""
import base64
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir; this dir is our own sparse checkout of main
import calm_static as C  # noqa: E402

MANIFEST = C.MANIFEST
LEDGER_REPO, LEDGER_PATH = "purpusgit/specs", "flutter-ui/approvals.tsv"
SPECS_LINK = re.compile(r"https://github\.com/purpusgit/specs/(?:blob|tree)/main/([^#?\s)>\]]+)")
WIREFRAME = re.compile(r"^[ \t>*-]*Wireframe:[ \t]*(\S.*?)[ \t]*$", re.M | re.I)
NO_VISUAL = re.compile(r"^[ \t>*-]*No visual change:[ \t]*(\S.*?)[ \t]*$", re.M | re.I)
KINDS = ("founder", "delegated")


class Unreadable(Exception):
    """The PR's files or the base manifest could not be read: fail closed (exit 2)."""


class Api:
    def __init__(self, token="", specs_token=""):
        self.token, self.specs_token = token, specs_token

    def _get(self, path, token):
        req = urllib.request.Request("https://api.github.com" + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "wireframe-link"})
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None

    def pr_files(self, repo, number):
        out = []
        for page in range(1, 31):
            st, data = self._get("/repos/%s/pulls/%s/files?per_page=100&page=%d" % (repo, number, page), self.token)
            if st != 200 or not isinstance(data, list):
                raise Unreadable("cannot read the PR's files (HTTP %s)" % st)
            out += [f["filename"] for f in data]
            if len(data) < 100:
                return out
        raise Unreadable("a PR with more than 3000 files")

    def contents(self, repo, path, ref, ledger=False):
        """-> text, or None when the file does not exist (404). Anything else unreadable raises Unreadable."""
        st, data = self._get("/repos/%s/contents/%s?ref=%s" % (repo, path, ref), self.specs_token if ledger else self.token)
        if st == 404 and not ledger:
            return None
        if st != 200 or not isinstance(data, dict) or "content" not in data:
            raise Unreadable("cannot read %s/%s@%s (HTTP %s)" % (repo, path, ref, st))
        return base64.b64decode(data["content"]).decode("utf-8", "replace")


def parse_ledger(text):
    rows = []
    for ln in (text or "").splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        c = [x.strip() for x in ln.split("\t")]
        if c[0].lower().startswith("frames") or len(c) < 3:
            continue
        m = SPECS_LINK.search(c[0])
        rows.append({"file": (m.group(1) if m else c[0]).lstrip("/"), "screens": {s.strip() for s in re.split(r"[,\s]+", c[1]) if s.strip()},
                     "kind": c[2].lower()})
    return rows


def protected(row, today):
    """A flavour home, or a flavour still inside new_until (an unreadable date counts as still new)."""
    if str(row.get("flavour_home", "")).lower() == "true":
        return "flavour home"
    if row.get("flavour"):
        try:
            if datetime.date.fromisoformat(row.get("new_until", "")) <= today:
                return ""
        except ValueError:
            pass
        return "new flavour (%s)" % row["flavour"]
    return ""


def judge(base_manifest, changed, body, ledger_text, repo_name, today):
    """-> dict(notice, findings, pending). ledger_text: str, or None when the ledger was unreadable."""
    if base_manifest is None:
        return {"notice": "no web-width manifest in this repo (%s) at the base: nothing to check, passing." % MANIFEST, "findings": [], "pending": []}
    _, rows = C.parse_manifest(base_manifest)
    hit = {n: r for n, r in rows.items() if r.get("file") in changed}
    if not hit:
        return {"notice": "", "findings": [], "pending": []}
    names = ", ".join(sorted(hit))
    findings, pending, notice = [], [], ""
    wf = WIREFRAME.search(body or "")
    nv = NO_VISUAL.search(body or "")
    nv_why = nv.group(1).strip() if nv else ""
    if not wf and not (len(nv_why) >= 3 and not re.fullmatch(r"<[^>]*>", nv_why)):
        findings.append("this PR changes %s: add `Wireframe: <specs main link>` or `No visual change: <why>` to the description" % names)
    if wf:
        m = SPECS_LINK.search(wf.group(1))
        if not m:
            findings.append("`Wireframe:` must be a https://github.com/purpusgit/specs/blob/main/... link")
        elif ledger_text is None:
            notice = "ledger unreadable: specs read access not set up (specs#449)"
        else:
            path = m.group(1)
            ledger = [r for r in parse_ledger(ledger_text) if r["file"] == path]
            if not ledger:
                findings.append("%s is not in %s: ask UX-00 to add its approval" % (path, LEDGER_PATH))
            else:
                missing = False
                for n, r in sorted(hit.items()):
                    rs = [x for x in ledger if x["kind"] in KINDS and x["screens"] & {n, "%s:%s" % (repo_name, n)}]
                    if not rs:
                        missing = True
                        findings.append("%s: the approval for %s lists other screens, not %s" % (n, path, n))
                    elif protected(r, today) and all(x["kind"] == "delegated" for x in rs):
                        findings.append("%s is a %s: a `delegated` approval never covers it, it needs a `founder` row" % (n, protected(r, today)))
                # spec: the row's screens must include EVERY changed screen, so one row has to hold them all
                if not missing and not any(x["kind"] in KINDS and all(x["screens"] & {n, "%s:%s" % (repo_name, n)} for n in hit) for x in ledger):
                    findings.append("no single ledger row for %s lists every changed screen (%s)" % (path, names))
    if nv and not wf:
        pending.append("No visual change: %s: pixel compare pending (harness piece)" % names)
    return {"notice": notice, "findings": findings, "pending": pending}


def render(res, enforce):
    lvl = "error" if enforce else "warning"
    lines = ["::%s title=ux-wireframe-link::%s" % (lvl, f) for f in res["findings"]]
    lines += ["::notice title=ux-wireframe-link::%s" % x for x in (res["notice"], *res["pending"]) if x]
    return "\n".join(lines)


def main(env=None, api=None, today=None):
    env = os.environ if env is None else env
    enforce = env.get("CALM_MODE", "warn") == "enforce"
    repo, number = env.get("REPO", ""), env.get("PR_NUMBER", "")
    api = api or Api(env.get("GITHUB_TOKEN", ""), env.get("SPECS_READ_TOKEN", ""))
    try:
        if not (repo and number and env.get("BASE_REF")):
            raise Unreadable("REPO, PR_NUMBER and BASE_REF are required")
        changed = set(api.pr_files(repo, number))
        base = api.contents(repo, MANIFEST, env["BASE_REF"])
        ledger, ledger_why = None, ""
        if WIREFRAME.search(env.get("PR_BODY", "")) and env.get("SPECS_READ_TOKEN"):
            try:
                ledger = api.contents(LEDGER_REPO, LEDGER_PATH, "main", ledger=True)
            except Unreadable as e:
                ledger_why = str(e)
        res = judge(base, changed, env.get("PR_BODY", ""), ledger, repo.split("/")[-1], today or datetime.date.today())
    except Unreadable as e:
        print("::error title=ux-wireframe-link::%s: failing closed" % e)
        return 2
    if res["notice"].startswith("ledger unreadable") and enforce:
        print("::error title=ux-wireframe-link::%s: failing closed in enforce" % res["notice"])
        return 2
    out = render(res, enforce)
    if res["notice"].startswith("ledger unreadable") and ledger_why:
        out += "\n(%s)" % ledger_why
    print(out or "ux-wireframe-link: nothing to check.")
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### ux-wireframe-link (%s)\n%s\n" % ("enforce" if enforce else "warn", "\n".join("- " + x for x in res["findings"] + res["pending"] + [res["notice"]] if x) or "nothing to check"))
    return 1 if (res["findings"] and enforce) else 0


if __name__ == "__main__":
    sys.exit(main())
