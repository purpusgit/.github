#!/usr/bin/env python3
"""CALM-1 static step (specs flutter-ui/calm-screen-gate-spec.md rev 5.1, section 3 "The static step"). WARN-ONLY unless CALM_MODE=enforce.

Run from a repo's root after `git fetch origin <base>`; BASE_REF (default origin/cwb). It does four things:

 1. NO ROW. A changed lib/**.dart file with a widget class whose `build` returns a Scaffold, Dialog, AlertDialog or
    DraggableScrollableSheet (a State<T> counts for T), or whose class is the `builder:` body of a showModalBottomSheet in the same
    file, and that has no row in the web-width manifest (test/UI/web_visual/manifest.yaml, rows keyed by file + class) -> a finding
    "add this screen to the web-width manifest". Exempt: lib/test_support/, and files that only CALL showDialog or
    showModalBottomSheet (a build inside such a call is not the file's own screen), and generated files.
 2. ENFORCE ROWS. The import closure through `package:<self>/` and relative imports (import, export, part). A row whose own file
    changed is FULL; a row whose file imports a changed file, directly or through others, is NEW-ONLY. `--emit-enforce` prints one
    line, `CALM_ENFORCE=<rows>` (row names, comma separated; `Row` is full, `Row:new` is new-findings-only), for the test job
    (`python3 calm_static.py --emit-enforce >> "$GITHUB_ENV"`). Nothing else goes to stdout in that mode.
 3. MANIFEST CHANGES. Rows are read from the BASE branch. A row added, removed, or with its file/build/flavour fields changed needs
    a line `calm-manifest-change: <row>: <why>` in the PR body (env PR_BODY).
 4. MODE. warn prints the findings (as ::warning::) and exits 0; CALM_MODE=enforce exits 1 on any finding.

No manifest in the repo (base and head) -> a notice and a pass. A base or diff that cannot be read fails closed (exit 2), in either
mode. The pure core (`analyze`) takes strings, so the replay can run it over API data.

shortcut: a lexical scan, not a Dart parser: a screen built by a helper function in another file is not seen, a build that wraps a
          Scaffold in a call named like show* is skipped, and the manifest is read with a small flat-YAML reader (rows with scalar
          fields; lists and maps under a row are ignored). Upgrade when a row needs a nested field.
Test: python3 scripts/test_calm_static.py
"""
import os
import posixpath
import re
import subprocess
import sys

MANIFEST = "test/UI/web_visual/manifest.yaml"
SCREEN_WIDGETS = ("Scaffold", "Dialog", "AlertDialog", "DraggableScrollableSheet")
SHOW_CALLS = r"(?:showDialog|showModalBottomSheet|showGeneralDialog|showCupertinoDialog|showAdaptiveDialog|showBottomSheet)"
ROW_FIELDS = ("file", "build", "flavour", "flavour_home", "new_until")  # a change to one of these needs the PR-body line
CHANGE_LINE = re.compile(r"^[ \t]*(?:[-*>][ \t]+)?calm-manifest-change:[ \t]*([\w./-]+)[ \t]*:[ \t]*(\S.*)$", re.M | re.I)


class Unreadable(Exception):
    """The diff, the base or a file could not be read: the step fails closed (exit 2)."""


# ── reading Dart ────────────────────────────────────────────────────────────────────────────────────────────────────
def mask(text):
    """text with comments and string contents blanked (same length, newlines kept). shortcut: simple/triple quotes and escapes only."""
    out, i, n = [], 0, len(text)
    while i < n:
        c, two = text[i], text[i:i + 2]
        if two == "//":
            j = text.find("\n", i); j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif two == "/*":
            j = text.find("*/", i + 2); j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j])); i = j
        elif c in "'\"":
            q = text[i:i + 3] if text[i:i + 3] in ("'''", '"""') else c
            j = i + len(q)
            while j < n and not text.startswith(q, j) and not (len(q) == 1 and text[j] == "\n"):
                j += 2 if text[j] == "\\" else 1
            end = min(j + len(q), n)
            out.append(q + "".join(ch if ch == "\n" else " " for ch in text[i + len(q):end - len(q)]) + (q if end - len(q) > i else ""))
            i = end
        else:
            out.append(c); i += 1
    return "".join(out)


def match_close(m, o):
    depth = 0
    for k in range(o, len(m)):
        if m[k] in "([{":
            depth += 1
        elif m[k] in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return -1


def stmt_end(m, a):
    depth = 0
    for k in range(a, len(m)):
        if m[k] in "([{":
            depth += 1
        elif m[k] in ")]}":
            if depth == 0:
                return k
            depth -= 1
        elif m[k] == ";" and depth == 0:
            return k
    return len(m)


def call_ranges(sk, name_re):
    """[(start, end)] of the argument lists of every call matching name_re."""
    out = []
    for mm in re.finditer(r"(?<![\w.])%s\s*(?:<[^()]*>)?\s*\(" % name_re, sk):
        c = match_close(sk, mm.end() - 1)
        if c > 0:
            out.append((mm.end() - 1, c))
    return out


def classes_of(sk):
    """[(name, extends, body_start, body_end)] for every class in the masked source."""
    out = []
    for mm in re.finditer(r"(?:^|\n)[ \t]*(?:(?:abstract|final|base|sealed|interface|mixin)\s+)*class\s+(\w+)(?:<[^{]*?>)?\s*(?:extends\s+([\w.]+(?:<[^{]*?>)?))?[^{;]*\{", sk):
        o = mm.end() - 1
        c = match_close(sk, o)
        if c > 0:
            out.append((mm.group(1), mm.group(2) or "", o, c))
    return out


def screen_classes(text):
    """{class: kind} for the screens this file defines: a build that holds a Scaffold/Dialog/AlertDialog/DraggableScrollableSheet
    outside a show*() call (kind 'build'), or the class a showModalBottomSheet builder returns, declared here (kind 'sheet')."""
    sk = mask(text)
    shows = call_ranges(sk, SHOW_CALLS)
    declared = {c[0] for c in classes_of(sk)}
    found = {}
    for name, ext, o, c in classes_of(sk):
        body = sk[o:c]
        for bm in re.finditer(r"\bWidget\s+build\s*\([^)]*\)\s*(=>|\{)", body):
            bo = o + bm.end() - 1 if bm.group(1) == "{" else o + bm.end()
            be = match_close(sk, bo) if bm.group(1) == "{" else stmt_end(sk, bo)
            for wm in re.finditer(r"(?<![\w.])(%s)\s*\(" % "|".join(SCREEN_WIDGETS), sk[bo:be]):
                at = bo + wm.start()
                if not any(a < at < b for a, b in shows):
                    target = name
                    sm = re.match(r"State\s*<\s*(\w+)", ext)
                    if sm:
                        target = sm.group(1)
                    found[target] = "build"
                    break
    for a, b in call_ranges(sk, r"showModalBottomSheet"):
        args = sk[a:b]
        bm = re.search(r"\bbuilder\s*:\s*\([^)]*\)\s*(?:=>|\{\s*(?:[^{}]*?;)*?\s*return)\s*(?:const\s+)?([A-Z]\w*)\s*\(", args)
        if bm and bm.group(1) in declared:
            found.setdefault(bm.group(1), "sheet")
    return found


def imports_of(text):
    """The quoted URIs of every import/export/part statement (conditional alternatives included)."""
    out = []
    for mm in re.finditer(r"^[ \t]*(?:import|export|part)\s+([^;]*);", text, re.M):
        out += re.findall(r"""['"]([^'"]+)['"]""", mm.group(1))
    return out


# ── the manifest ─────────────────────────────────────────────────────────────────────────────────────────────────────
def parse_manifest(text):
    """(package, {row: {field: value}}) from a web-width manifest: `screens:` map, rows at indent 2, scalar fields at indent 4."""
    package, rows, cur, in_screens, lines = "", {}, None, False, (text or "").splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip())
        t = ln.strip()
        if ind == 0:
            in_screens = t.startswith("screens:")
            cur = None
            if t.startswith("package:"):
                package = t.partition(":")[2].strip().strip("\"'")
            continue
        if not in_screens or t.startswith("- "):
            continue
        k, _, v = t.partition(":")
        v = v.strip()
        if ind == 2 and not v:
            cur = rows.setdefault(k.strip(), {})
        elif cur is not None and ind == 4 and k.strip():
            if v[:1] in ("|", ">"):
                block = []
                while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > ind):
                    block.append(lines[i].strip()); i += 1
                v = " ".join(x for x in block if x)
            elif v[:1] not in ("\"", "'"):
                v = re.split(r"\s#", v, maxsplit=1)[0].strip()
            cur[k.strip()] = v.strip("\"'") if v[:1] in ("\"", "'") and v[-1:] == v[:1] else v
    return package, rows


def row_class(name, build, file_text):
    """The class a row stands for: the row name if the row's file declares it, else the first class its build names that the file declares."""
    declared = {c[0] for c in classes_of(mask(file_text or ""))}
    if name in declared or not declared:
        return name
    for cand in re.findall(r"\b([A-Z]\w*)\s*\(", build or ""):
        if cand in declared:
            return cand
    return name


# ── the pure core ────────────────────────────────────────────────────────────────────────────────────────────────────
def analyze(base_manifest, head_manifest, changed, read_head, lib_files, pr_body="", self_pkg=""):
    """-> dict(notice, no_row, manifest, enforce). changed: {path: 'A'|'M'|'D'}; read_head(path) -> text or None;
    lib_files: every lib/**.dart path at the head; base_manifest/head_manifest: the YAML text or None."""
    if base_manifest is None and head_manifest is None:
        return {"notice": "no web-width manifest in this repo (%s) yet: nothing to check, passing." % MANIFEST, "no_row": [], "manifest": [], "enforce": []}
    pkg_h, head_rows = parse_manifest(head_manifest or "")
    pkg_b, base_rows = parse_manifest(base_manifest or "")
    self_pkg = self_pkg or pkg_h or pkg_b
    live = {p: s for p, s in changed.items() if s != "D" and p.startswith("lib/") and p.endswith(".dart") and not p.endswith((".g.dart", ".freezed.dart"))}

    head_keys = {(r.get("file", ""), row_class(n, r.get("build", ""), read_head(r.get("file", "")))) for n, r in head_rows.items()}
    no_row = []
    for path in sorted(live):
        if path.startswith("lib/test_support/"):
            continue
        text = read_head(path)
        if text is None:
            continue
        for cls, kind in sorted(screen_classes(text).items()):
            if (path, cls) not in head_keys:
                no_row.append((path, cls, kind))

    # manifest changes: keyed by file + class; the base's own file text gives the base rows' class
    def key(rows, read):
        out = {}
        for n, r in rows.items():
            f = r.get("file", "")
            out[(f, row_class(n, r.get("build", ""), read(f)))] = (n, r)
        return out
    hk, bk = key(head_rows, read_head), key(base_rows, lambda f: read_head(f))
    wanted = {m.group(1): m.group(2).strip() for m in CHANGE_LINE.finditer(pr_body or "")
              if len(m.group(2).strip()) >= 3 and not re.fullmatch(r"<[^>]*>", m.group(2).strip())}
    manifest = []
    for k in sorted(set(hk) | set(bk)):
        if k in hk and k not in bk:
            what, row = "added", hk[k][0]
        elif k in bk and k not in hk:
            what, row = "removed", bk[k][0]
        else:
            diff = [f for f in ROW_FIELDS if hk[k][1].get(f) != bk[k][1].get(f)]
            if not diff:
                continue
            what, row = "changed (%s)" % ", ".join(diff), hk[k][0]
        cover = {row, k[1]} & set(wanted)
        if not cover:
            manifest.append((row, k[0], what))

    # import closure
    importers, rows_by_file = {}, {}
    for p in lib_files:
        text = read_head(p)
        for uri in imports_of(text or ""):
            tgt = None
            if uri.startswith("package:"):
                pk, _, rest = uri[8:].partition("/")
                if pk == self_pkg:
                    tgt = "lib/" + rest
            elif "://" not in uri and not uri.startswith("dart:"):
                tgt = posixpath.normpath(posixpath.join(posixpath.dirname(p), uri))
            if tgt:
                importers.setdefault(tgt, set()).add(p)
    affected, queue = set(), list(live)
    while queue:
        cur = queue.pop()
        for imp in importers.get(cur, ()):
            if imp not in affected:
                affected.add(imp); queue.append(imp)
    enforce = []
    for n, r in sorted(head_rows.items()):
        f = r.get("file", "")
        if f in live:
            enforce.append(n)
        elif f in affected:
            enforce.append(n + ":new")
    return {"notice": "", "no_row": no_row, "manifest": manifest, "enforce": enforce}


# ── git around it ────────────────────────────────────────────────────────────────────────────────────────────────────
def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Unreadable("git %s failed: %s" % (" ".join(args[:3]), (r.stderr or "").strip()[:200]))
    return r


def gather(base, root="."):
    git("rev-parse", "--verify", "-q", base + "^{commit}")
    base_m = git("show", "%s:%s" % (base, MANIFEST), check=False)
    base_manifest = base_m.stdout if base_m.returncode == 0 else None
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as fh:
            head_manifest = fh.read()
    except OSError:
        head_manifest = None
    diff = git("diff", "--name-status", "--no-renames", "--no-color", base + "...HEAD", "--", "lib").stdout
    changed = {}
    for ln in diff.splitlines():
        parts = ln.split("\t")
        if len(parts) == 2:
            changed[parts[1]] = parts[0][:1]
    lib_files = [p for p in git("ls-files", "lib").stdout.splitlines() if p.endswith(".dart")]
    cache = {}

    def read_head(p):
        if p not in cache:
            try:
                with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                    cache[p] = fh.read()
            except OSError:
                cache[p] = None
        return cache[p]
    pubspec = read_head("pubspec.yaml") or ""
    mm = re.search(r"^name:\s*(\S+)", pubspec, re.M)
    return base_manifest, head_manifest, changed, read_head, lib_files, (mm.group(1) if mm else "")


def render(res, enforce_mode):
    lvl = "error" if enforce_mode else "warning"
    out = []
    for path, cls, kind in res["no_row"]:
        what = "a sheet body" if kind == "sheet" else "builds a Scaffold, Dialog or sheet"
        out.append("::%s file=%s::CALM-STATIC %s (%s) has no row in %s: add this screen to the web-width manifest" % (lvl, path, cls, what, MANIFEST))
    for row, f, what in res["manifest"]:
        out.append("::%s file=%s::CALM-STATIC manifest row %s was %s: add a line to the PR body: calm-manifest-change: %s: <why>" % (lvl, MANIFEST, row, what, row))
    return out


def main(argv):
    emit = "--emit-enforce" in argv
    enforce_mode = os.environ.get("CALM_MODE", "warn").strip().lower() == "enforce"
    base = os.environ.get("BASE_REF") or "origin/cwb"
    try:
        bm, hm, changed, read_head, lib_files, pkg = gather(base)
        res = analyze(bm, hm, changed, read_head, lib_files, os.environ.get("PR_BODY", ""), pkg)
    except Unreadable as e:
        print("::error::CALM-STATIC could not read its inputs (%s). This is a gate failure, not a pass." % e, file=sys.stderr if emit else sys.stdout)
        return 2
    if emit:
        print("CALM_ENFORCE=" + ",".join(res["enforce"]))
        return 0
    if res["notice"]:
        print("CALM-STATIC notice: " + res["notice"])
        return 0
    for ln in render(res, enforce_mode):
        print(ln)
    print("CALM-STATIC (%s): %d screen file(s) with no manifest row, %d manifest row change(s) without a calm-manifest-change line; %d row(s) to enforce: %s."
          % ("enforce" if enforce_mode else "warn only", len(res["no_row"]), len(res["manifest"]), len(res["enforce"]), ",".join(res["enforce"]) or "none"))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and (res["no_row"] or res["manifest"]):
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("### CALM-1 static step (%s)\n\n" % ("enforce" if enforce_mode else "warn only") + "\n".join("- " + x.split("::", 2)[-1] for x in render(res, enforce_mode)) + "\n")
        except OSError:
            pass
    return 1 if (enforce_mode and (res["no_row"] or res["manifest"])) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
