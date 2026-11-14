#!/usr/bin/env python3
"""ux-layout gate: a layout mistake we already fixed must not come back (founder, 14 Nov 2026).

On 14 Nov dev.prabhupad.org's Explore Organizations lost 40 px a side on a phone: PageContentWidth was
given a hard-coded 24 gutter on top of the 16 each section adds. It was fixed and written down a year
ago and nothing stopped it returning. This gate fails a PR that ADDS one of these in lib/**.dart
(rule ids follow UX-01's specs#1373 README):

  W2   PageContentWidth( without `kind:`
  W3   PageContentWidth( with a hard-coded non-zero `padding:` (the double gutter). EdgeInsets.zero is fine.
  W1   a raw BoxConstraints(maxWidth: N) cap in a screen file (lib/screens/, *_screen.dart, *_page.dart)
  W10  `outlineVariant` on a text-field border (InputBorder, InputDecoration, TextField ...)
  W11  ScreenUtil in a *_web.dart file (16.w, 12.sp, ScreenUtil())
  W12  ScreenUtil.init( / ScreenUtilInit( outside pkg_orbit_binder
  DBG  a debug-only control (a bug icon, a /dev or /playground route, a Dev/Debug/Playground screen) with
       no kDebugMode in the same declaration

Only lines the PR adds count. Today's code passes until someone touches the line. For a construct that
spans lines (a PageContentWidth( call) the finding sits on the line that carries the problem: the call's
first line for W2, the `padding:` line for W3, the `maxWidth:` line for W1.
shortcut: a PR that only REMOVES `kind:` from an existing call is not seen (no added line); the call is
          caught the next time someone adds a line to it. Upgrade by scanning every changed call.

A deliberate exception carries `// layout-ok: <why>` on the line or the line above it (on the construct's
first line too). An empty reason does not count.

Env: BASE_REF (default origin/cwb), GITHUB_REPOSITORY (to tell pkg_orbit_binder).
Exit 0 clean, 1 on findings, 2 when the diff cannot be computed (a gate failure, never a clean pass).
Test: python3 scripts/test_ux_layout.py
"""
import bisect
import os
import re
import subprocess
import sys

BINDER = {"pkg_orbit_binder", "orbit_binder"}  # pubspec names / repo name that may own ScreenUtil.init
LAYOUT_OK = re.compile(r"//\s*layout-ok:\s*\S")
SCREEN_PATH = re.compile(r"(^|/)screens?/|[._]screen\.dart$|[._]page\.dart$")
WEB_PATH = re.compile(r"[._]web\.dart$")
FIELD_CALLEES = {"OutlineInputBorder", "UnderlineInputBorder", "InputBorder", "InputDecoration",
                 "InputDecorationTheme", "TextField", "TextFormField", "DropdownButtonFormField",
                 "DropdownMenu", "SearchBar", "SearchAnchor"}
BORDER_LABELS = {"enabledBorder", "focusedBorder", "disabledBorder", "errorBorder", "focusedErrorBorder", "border"}
QUIET_LABELS = {"disabledBorder", "errorBorder", "focusedErrorBorder"}
SCALE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\.(?:w|h|sp|r|sw|sh)\b|\bScreenUtil\s*(?:\(|\.)")
SU_INIT = re.compile(r"\bScreenUtil\.init\s*\(|\bScreenUtilInit\s*\(")
DBG_ICON = re.compile(r"\b(?:Icons|CupertinoIcons)\.(?:bug_report\w*|bug\w*|ant\w*|developer_mode\w*)\b")
DBG_ROUTE = re.compile(r"""['"]/(?:dev|debug|playground|devtools|dev-tools)(?:[/?'"]|$)""", re.I)
DBG_NAME = re.compile(r"\b\w*(?:Dev|Debug|Playground)\w*(?:Screen|Page)\b")
DBG_DECL = re.compile(r"^\s*(?:abstract\s+|final\s+)*class\s")
DBG_FLAVOR = re.compile(r"\bAppFlavor\.\w*dev\w*|\bisDev(?:elopment)?\b", re.I)
DBG_USER_FEATURE = re.compile(r"report|feedback", re.I)  # a user-facing "report a bug" row is not a debug control
DEV_PATH = re.compile(r"(^|/)(?:dev|debug|playground)/")  # a dev tool's own files; its route registration is judged
TEST_SUPPORT = re.compile(r"(^|/)test_support/")
TYPE_DECL = re.compile(r"^\s*(?:abstract\s+|final\s+|base\s+|sealed\s+)*(?:class|mixin|extension|enum)\b")

FIX = {
    "W2": "PageContentWidth has no default width. Pass kind: ContentWidth.auth|reading|content|full "
          "(see pkg_orbit_client_core lib/UI/page_content_width.widget.dart).",
    "W3": "PageContentWidth already adds the gutter (16 under 600 px, 24 above). A hard-coded padding stacks "
          "on the 16 each section adds and doubles it. Drop padding:, or pass EdgeInsets.zero when the body owns its gutter.",
    "W1": "Do not cap the width yourself. Wrap the body in PageContentWidth(kind: ContentWidth.reading|content|auth) "
          "so every screen shares one column width.",
    "W10": "A text field's border must not use outlineVariant (too faint on web). Use the theme's "
           "inputDecorationTheme border, or colorScheme.outline.",
    "W11": "No ScreenUtil in a *_web.dart file: web sizes are raw logical px. Write 16 and 12, not 16.w and 12.sp.",
    "W12": "ScreenUtil.init and ScreenUtilInit belong to pkg_orbit_binder only. Do not initialise it here.",
    "DBG": "A debug-only control must sit under kDebugMode: if (kDebugMode) <control>, so it never ships.",
}


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def mask(text):
    """text with comments and string contents blanked (same length, newlines kept), so brackets and
    names inside them are not mistaken for code. shortcut: no raw/triple-quote strings with embedded
    quotes; at worst a few characters are unmasked and a rule sees extra text."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
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


def match_close(m, open_idx):
    """Index of the bracket that closes the one at open_idx in masked text m, or -1."""
    depth = 0
    for k in range(open_idx, len(m)):
        if m[k] in "([{":
            depth += 1
        elif m[k] in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return -1


def args_of(m, open_idx, close_idx):
    """[(name or None, value_start, value_end)] for the top-level arguments between the parens."""
    out, depth, start = [], 0, open_idx + 1
    for k in range(open_idx + 1, close_idx + 1):
        ch = m[k] if k < close_idx else ","
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and k < close_idx:
            depth -= 1
        elif ch == "," and depth == 0:
            seg = m[start:k]
            lab = re.match(r"\s*([A-Za-z_]\w*)\s*:(?!:)", seg)
            if seg.strip():
                out.append((lab.group(1) if lab else None, start + (lab.end() if lab else 0), k))
            start = k + 1
    return out


def enclosing(m, idx):
    """[(callee, label, open_idx)] for every unclosed '(' around idx, innermost first. label is the
    `name:` written in front of the callee, e.g. enabledBorder in `enabledBorder: Foo(...)`, else None."""
    out, depth = [], 0
    for k in range(idx, -1, -1):
        ch = m[k]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth:
                depth -= 1
            elif ch == "(":
                head = re.search(r"(?:([A-Za-z_]\w*)\s*:\s*)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*$", m[max(0, k - 80):k])
                if head:
                    out.append((head.group(2).split(".")[0], head.group(1), k))
    return out


def member_start(lines, idx):
    """Index of the line that opens the top-level or class-member declaration holding idx."""
    depth, before = 0, []
    for s in lines[: idx + 1]:
        before.append(depth)
        code = s.split("//")[0]
        depth = max(0, depth + code.count("{") - code.count("}"))
    tops = [i for i in range(idx + 1) if before[i] == 0]
    top = tops[-1] if tops else 0
    if before[idx] >= 2 and TYPE_DECL.search(lines[top]):
        members = [i for i in range(top + 1, idx + 1) if before[i] == 1]
        return members[-1] if members else top
    return top


def scan_text(path, text, added=None, module=""):
    """[(line, rule, snippet)] for the findings in one file. added: the set of 1-based line numbers the
    change adds (None = every line, for a backlog report). module: the pubspec name or repo name."""
    m, lines = mask(text), text.splitlines()
    nl = [i for i, ch in enumerate(m) if ch == "\n"]

    def line_of(idx):
        return bisect.bisect_left(nl, idx) + 1

    found = []

    def add(line, rule, first=None):
        """first: the construct's first line, where a layout-ok note is also accepted."""
        if added is not None and line not in added:
            return
        for ln in {line, first or line}:
            if ok_note(lines, ln - 1):
                return
        found.append((line, rule, lines[line - 1].strip()[:110]))

    # W2, W3: PageContentWidth( ... )
    for mm in re.finditer(r"(?<![\w.])PageContentWidth\s*\(", m):
        o = mm.end() - 1
        c = match_close(m, o)
        if c < 0:
            continue
        args, first = args_of(m, o, c), line_of(mm.start())
        if re.search(r"\b(?:this|super)\.\w+|\brequired\b", m[o:c]):  # the constructor declaration, not a use
            continue
        if "kind" not in {a for a, _, _ in args}:
            add(first, "W2")
        for name, s, e in args:
            if name == "padding" and hard_gutter(text[s:e]):
                add(line_of(s), "W3", first)

    # W1: a raw page-width cap in a screen file: a literal of at least 320 (a column), or a constant such as
    # _contentMaxWidth / kMaxWidth. Not a cap derived from the parent (box.maxWidth * 0.6), not a local
    # variable (a chat bubble's maxWidth) and not a small literal (a 160 px label), which are local sizing.
    if SCREEN_PATH.search(path) and not DEV_PATH.search(path):
        for mm in re.finditer(r"(?<![\w.])BoxConstraints\s*\(", m):
            o = mm.end() - 1
            c = match_close(m, o)
            if c < 0:
                continue
            for name, s_, e in args_of(m, o, c):
                if name == "maxWidth" and page_cap(text[s_:e]):
                    add(line_of(s_), "W1", line_of(mm.start()))

    # W10: outlineVariant on a text field's RESTING or FOCUSED border. disabledBorder / errorBorder may be faint or
    # coloured. A bare `border:` only counts when the decoration has no enabledBorder (else it is never shown).
    for mm in re.finditer(r"\boutlineVariant\b", m):
        chain = enclosing(m, mm.start())
        if not any(c in FIELD_CALLEES for c, _, _ in chain):
            continue
        label = next((lb for _, lb in [(c, lb) for c, lb, _ in chain] if lb in BORDER_LABELS), None)
        if label in QUIET_LABELS:
            continue
        if label == "border":
            deco = next(((o) for c, _, o in chain if c in ("InputDecoration", "InputDecorationTheme")), None)
            c2 = match_close(m, deco) if deco is not None else -1
            if c2 > 0 and "enabledBorder" in {n for n, _, _ in args_of(m, deco, c2)}:
                continue
        add(line_of(mm.start()), "W10")

    # W11, W12: ScreenUtil
    mlines = m.splitlines()
    for i, ln in enumerate(mlines, 1):
        if WEB_PATH.search(path) and SCALE.search(ln):
            add(i, "W11")
        if SU_INIT.search(ln) and not TEST_SUPPORT.search(path) and not any(b in module for b in BINDER):
            add(i, "W12")

    # DBG: a debug-only control with no kDebugMode (or dev-flavor check) in the same declaration
    for i, raw in enumerate(lines):
        if DEV_PATH.search(path):
            break
        code = mlines[i] if i < len(mlines) else ""
        if DBG_DECL.search(code):
            continue
        if DBG_ICON.search(code) or DBG_NAME.search(code) or DBG_ROUTE.search(raw.split("//")[0]):
            if DBG_USER_FEATURE.search(DBG_ICON.sub("", "\n".join(lines[max(0, i - 6): i + 7]))):
                continue
            start = member_start(lines, i)
            if not any(re.search(r"\bk(?:Debug|Release)Mode\b|\bassert\s*\(", lines[j].split("//")[0])
                       or DBG_FLAVOR.search(lines[j].split("//")[0]) for j in range(start, i + 1)):
                add(i + 1, "DBG")
    return sorted(set(found))


def ok_note(lines, idx):
    """True if line idx (0-based) or the comment line above it carries `// layout-ok: <why>`."""
    if idx < 0 or idx >= len(lines):
        return False
    if LAYOUT_OK.search(lines[idx]):
        return True
    return idx > 0 and lines[idx - 1].lstrip().startswith("//") and bool(LAYOUT_OK.search(lines[idx - 1]))


def page_cap(value):
    """True if a maxWidth: value is a page-column cap: a literal >= 320 (optionally .w) or a constant name
    (leading _ or k, or capitalised) that mentions Width. shortcut: 320 is a guess at 'wider than a label'; tune it
    from the replay table in the PR if a real cap sits below it."""
    v = re.sub(r"//.*", "", value).strip().rstrip(",")
    lit = re.fullmatch(r"(?:const\s+)?(\d+(?:\.\d+)?)(?:\.w)?", v)
    if lit:
        return float(lit.group(1)) >= 320
    return bool(re.fullmatch(r"(?:_|k[A-Z]|[A-Z])\w*", v) and re.search(r"[Ww]idth", v))


def hard_gutter(value):
    """True if a padding: value hard-codes a non-zero horizontal inset: EdgeInsets.symmetric(horizontal: 24),
    .all(24), .only(left: 8) ... Vertical-only insets and EdgeInsets.zero are fine."""
    v = re.sub(r"\b(?:vertical|top|bottom)\s*:\s*[\w.]+", "", re.sub(r"//.*", "", value))
    return bool(re.search(r"(?<![\w.])(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)(?![\w])", v))


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


def module_name(root="."):
    try:
        with open(os.path.join(root, "pubspec.yaml"), encoding="utf-8") as fh:
            mm = re.search(r"^name:\s*(\S+)", fh.read(), re.M)
    except OSError:
        mm = None
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
    return (mm.group(1) if mm else "") + " " + repo


def gate(base):
    try:
        git("rev-parse", "--verify", "-q", base + "^{commit}")
        files = added_lines(base)
    except subprocess.CalledProcessError as e:
        print("::error::ux-layout could not diff against " + base + ": " + (e.stderr or "").strip()
              + ". This is a gate failure, not a clean tree.")
        return 2
    module, findings = module_name(), []
    for path, lines in sorted(files.items()):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        findings += [(path, *f) for f in scan_text(path, text, lines, module)]
    print("ux-layout: checked the lines " + str(sum(len(s) for s in files.values())) + " added in "
          + str(len(files)) + " lib/ file(s) against " + base + ".")
    for path, n, rule, snippet in findings:
        print("::error file=" + path + ",line=" + str(n) + "::" + rule + ": " + FIX[rule])
        print(path + ":" + str(n) + ": " + rule + ": " + snippet)
    if findings:
        for rule in sorted({r for _, _, r, _ in findings}):
            print(rule + " fix: " + FIX[rule])
        print("\n" + str(len(findings)) + " layout blunder(s). A deliberate exception: add `// layout-ok: <why>` "
              "on the line or the line above it.")
        return 1
    print("ux-layout passed.")
    return 0


if __name__ == "__main__":
    sys.exit(gate(os.environ.get("BASE_REF") or "origin/cwb"))
