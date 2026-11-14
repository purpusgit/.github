#!/usr/bin/env python3
"""ux-one-template gate: one app frame, and it is pkg_orbit_client_core's central shell (UX-00, specs#411 row 5).

A module repo must not build a second app frame. This fails a PR that ADDS, in lib/**.dart of any repo except
pkg_orbit_client_core:
  - a NavigationRail( , NavigationBar( or BottomNavigationBar( call, or
  - a class whose name contains Shell or SideMenu AND that is a widget (extends ...Widget or GetView).
    Plain classes such as FooShellContributions (how a module plugs INTO the one shell), controllers, middleware and
    data types are not frames and pass. A State<...> companion is not counted; its widget class is.

Today's copies are listed in scripts/ux_one_template_allowlist.txt, as `<repo> <path> <Name>` lines. The list lives in
purpusgit/.github and is fetched with this script from main, so a PR cannot extend it. It can only shrink: the
self-test fails if an entry is added (scripts/test_ux_one_template.py). There is NO inline escape comment for the
same reason. To change the shell itself, change pkg_orbit_client_core.

Only lines the PR adds count. A moved or renamed frame is a new pair and fails until UX-01 agrees to a list edit.
shortcut: the widget check is by name and `extends`; a frame built from a plain function or a Scaffold in a class
          with no Shell/SideMenu in its name is not seen. Upgrade when one appears in the replay.
shortcut: mask() and added_lines() are copies of scripts/ux_layout.py (that file is not on main yet); merge them
          into one module when both gates are on main.

Env: BASE_REF (default origin/cwb), GITHUB_REPOSITORY (the repo's name, to find its allowlist lines).
Exit 0 clean, 1 on findings, 2 when the diff cannot be computed (a gate failure, never a clean pass).
Test: python3 scripts/test_ux_one_template.py
"""
import bisect
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWLIST = os.path.join(HERE, "ux_one_template_allowlist.txt")
EXEMPT = {"pkg_orbit_client_core", "orbit_client_core"}  # the one shell lives here
FRAME_CALL = re.compile(r"(?<![\w.])(NavigationRail|NavigationBar|BottomNavigationBar)\s*\(")
CLASS_DECL = re.compile(r"^[ \t]*(?:(?:abstract|final|base|sealed)\s+)*class\s+(\w*(?:Shell|SideMenu)\w*)([^{;]*)\{", re.M)
WIDGET_BASE = re.compile(r"\bextends\s+(?:\w+\.)?(?:\w*Widget|GetView|GetWidget)\b")
FIX = ("There is one app frame: pkg_orbit_client_core's shell (lib/shell/). Add your module's tabs and entries with a "
       "ShellContributions class instead of building another NavigationRail, NavigationBar, BottomNavigationBar or "
       "*Shell* / *SideMenu* widget. To change the shell itself, change client_core. There is no inline escape; "
       "ask UX-01 to change scripts/ux_one_template_allowlist.txt in purpusgit/.github.")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def mask(text):
    """text with comments and string contents blanked (same length, newlines kept)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c, two = text[i], text[i:i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j])); i = j
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c + "".join(ch if ch == "\n" else " " for ch in text[i + 1:j - 1]) + (c if j - 1 > i else ""))
            i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def load_allowlist(path=ALLOWLIST):
    """{(repo, path, name)} from the allowlist file; a missing file is an error, never an empty list."""
    out = set()
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.split("#", 1)[0].strip()
            if ln:
                parts = ln.split()
                if len(parts) != 3:
                    raise ValueError("allowlist line is not `<repo> <path> <Name>`: " + ln)
                out.add(tuple(parts))
    return out


def scan_text(path, text, added=None, repos=(), allow=frozenset()):
    """[(line, kind, name)] for the frames one file adds. repos: the names this repo goes by (repo name, pubspec
    name). added: 1-based line numbers the change adds (None = all, for a backlog report)."""
    if EXEMPT & set(repos):
        return []
    m = mask(text)
    nl = [i for i, ch in enumerate(m) if ch == "\n"]
    found = []

    def add(idx, kind, name):
        line = bisect.bisect_left(nl, idx) + 1
        if (added is None or line in added) and not any((r, path, name) in allow for r in repos):
            found.append((line, kind, name))

    for mm in FRAME_CALL.finditer(m):
        add(mm.start(), "widget", mm.group(1))
    for mm in CLASS_DECL.finditer(m):
        if WIDGET_BASE.search(mm.group(2)):
            add(mm.start(1), "class", mm.group(1))
    return sorted(set(found))


def added_lines(base):
    """{path: set of added line numbers} for lib/**.dart, from `git diff -U0 base...HEAD`."""
    diff = git("diff", "-U0", "--no-color", "--diff-filter=ACMR", base + "...HEAD", "--", "lib")
    out, cur = {}, None
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            cur = ln[6:] if ln.startswith("+++ b/") else None
            if cur and (not cur.endswith(".dart") or cur.endswith((".g.dart", ".freezed.dart"))):
                cur = None
            if cur:
                out.setdefault(cur, set())
        elif ln.startswith("@@") and cur:
            mm = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", ln)
            if mm:
                start, n = int(mm.group(1)), int(mm.group(2) if mm.group(2) is not None else 1)
                out[cur].update(range(start, start + n))
    return {p: s for p, s in out.items() if s}


def repo_names(root="."):
    names = [os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]]
    try:
        with open(os.path.join(root, "pubspec.yaml"), encoding="utf-8") as fh:
            mm = re.search(r"^name:\s*(\S+)", fh.read(), re.M)
        names.append(mm.group(1) if mm else "")
    except OSError:
        pass
    return tuple(n for n in names if n)


def gate(base):
    try:
        git("rev-parse", "--verify", "-q", base + "^{commit}")
        files = added_lines(base)
        allow = load_allowlist(ALLOWLIST)
    except (subprocess.CalledProcessError, OSError, ValueError) as e:
        print("::error::ux-one-template could not run: " + str(getattr(e, "stderr", None) or e).strip()
              + ". This is a gate failure, not a clean tree.")
        return 2
    repos, findings = repo_names(), []
    for path, lines in sorted(files.items()):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        findings += [(path, *f) for f in scan_text(path, text, lines, repos, allow)]
    print("ux-one-template: checked the lines added in " + str(len(files)) + " lib/ file(s) against " + base
          + ("; client_core is exempt." if EXEMPT & set(repos) else "."))
    for path, n, kind, name in findings:
        what = name + "( is a second app frame" if kind == "widget" else "class " + name + " is a second app frame"
        print("::error file=" + path + ",line=" + str(n) + "::ONE-TEMPLATE: " + what)
        print(path + ":" + str(n) + ": " + what)
    if findings:
        print("\n" + str(len(findings)) + " second app frame(s). " + FIX)
        return 1
    print("ux-one-template passed.")
    return 0


if __name__ == "__main__":
    sys.exit(gate(os.environ.get("BASE_REF") or "origin/cwb"))
