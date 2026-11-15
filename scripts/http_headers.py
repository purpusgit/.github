#!/usr/bin/env python3
"""http-headers gate: no request header may carry a value we cannot prove is ISO-8859-1 (PRABHUPAD-01, specs#411).

Why: on web, `fetch` throws "String contains non ISO-8859-1 code point" for a header value outside Latin-1 and every call fails.
`applyOrgHeaders` copied an org's short name ("Śrīla Prabhupāda") into `x-org-short-name`; client_core#1060 added the guard
`isHeaderSafe`. This gate keeps free-text values out of headers everywhere else.

RULE A (every Dart repo; lib/**.dart; ADDED lines only): setting a request header to a NON-LITERAL value fails. Seen as:
  headers[k] = <expr>   headers.putIfAbsent(k, () => <expr>)   headers.set(k, <expr>) / .add(k, <expr>)
  headers.addAll({k: <expr>}) / headers.addAll(<expr>)   and a `headers: {k: <expr>}` / `headers: <expr>` argument (dio, http, fetch).
  OK: a string literal with no `$` interpolation, a `const`, a header-constant (kFooHeader, SCREAMING_CASE, Headers.x, HttpHeaders.x),
  null/true/false/a number, and an a ? b : c / a ?? b whose branches are OK. NOT OK: an identifier, a call, or a string that
  interpolates `$x`: we cannot prove it is ASCII. Exempt directory (default client_core's lib/http/, where the OrbitHttp
  interceptor and applyOrgHeaders live): HTTP_HEADERS_EXEMPT, `repo:prefix,repo:prefix`.
  Escape: `// header-ok: <why>` (non-empty) on the line or the line above, for a value proven ASCII (an id field).

RULE B (pkg_orbit_client_core only; whole files at the PR head, not just added lines): the Latin-1 guard must stay.
  lib/http/org_headers.dart must define `bool isHeaderSafe(` that tests for 0xFF, and the `x-org-short-name` write must go through it;
  both interceptors (_http_utils_web.dart, _http_utils_mobile.dart) must still call `applyOrgHeaders(`. No escape. A missing pinned
  file fails closed: if a file is renamed, update PINS here in purpusgit/.github.

shortcut: this is a lexical scan, not a Dart parser; a header set through a helper in another file is not seen, a conditional's
          test is judged like a value, and `headers.addAll(x)` of an already-built map is flagged (use header-ok if x is checked).
Env: BASE_REF (default origin/cwb), GITHUB_REPOSITORY, HTTP_HEADERS_EXEMPT, HTTP_HEADERS_ASCII_NAMES.
Exit 0 clean, 1 on findings, 2 when the diff cannot be computed or a pinned file is missing. Test: python3 scripts/test_http_headers.py
"""
import bisect
import os
import re
import subprocess
import sys

CORE = {"pkg_orbit_client_core", "orbit_client_core"}
DEFAULT_EXEMPT = "pkg_orbit_client_core:lib/http/"
# Identifiers the repo vouches are ASCII by construction (a client-made UUID, a MIME type): HTTP_HEADERS_ASCII_NAMES=name,name.
# Empty by default: whether `idempotencyKey` or `contentType` may pass is a policy call, not the scanner's.
def ascii_names():
    return {x.strip() for x in os.environ.get("HTTP_HEADERS_ASCII_NAMES", "").split(",") if x.strip()}


HEADER_OK = re.compile(r"//\s*header-ok:\s*\S")
CONST_CLASSES = r"(?:Headers|HttpHeaders|HeaderValues|HeaderConstants|ContentType)"
CONST_NAME = re.compile(r"^(?:[A-Za-z_]\w*\.)*(?:_?k[A-Z]\w*|[A-Z][A-Z0-9_]{2,})$|^" + CONST_CLASSES + r"\.\w+(?:\.\w+)*$")
# Calls whose result is provably ASCII: a UUID, a MIME type from the mime package, the decimal text of a length. Edit here.
SAFE_CALLS = [r"(?:const\s+)?Uuid\(\)\.v[145]\(\)", r"lookupMimeType\(.*\)", r"[\w.]*[lL]ength\.toString\(\)"]
# Rule B: (file, header, guard). Edit here if the interceptor moves.
PINS = {"file": "lib/http/org_headers.dart", "guard": "isHeaderSafe", "header": "x-org-short-name",
        "interceptors": ["lib/http/_http_utils_web.dart", "lib/http/_http_utils_mobile.dart"], "applier": "applyOrgHeaders("}
FIX_A = ("Do not put a value you cannot prove is ISO-8859-1 into a request header: a browser's fetch throws on any character above U+00FF "
         "and every call fails. Pass a literal or a header constant, or guard the value with isHeaderSafe(...) from client_core's "
         "lib/http/org_headers.dart. If the value is proven ASCII (an id), add `// header-ok: <why>` on that line.")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def lex(text):
    """(code, skel): code = text with comments blanked; skel = code with string contents blanked too (same length, newlines kept).
    shortcut: simple and triple quotes, \\ escapes and raw strings; `${...}` nesting is skipped by bracket depth."""
    code, skel, i, n = [], [], 0, len(text)
    blank = lambda s: "".join(ch if ch == "\n" else " " for ch in s)
    while i < n:
        c, two = text[i], text[i:i + 2]
        if two == "//":
            j = text.find("\n", i); j = n if j < 0 else j
            code.append(" " * (j - i)); skel.append(" " * (j - i)); i = j
        elif two == "/*":
            j = text.find("*/", i + 2); j = n if j < 0 else j + 2
            code.append(blank(text[i:j])); skel.append(blank(text[i:j])); i = j
        elif c in "'\"":
            raw = i > 0 and text[i - 1] in "rR" and (i < 2 or not text[i - 2].isalnum())
            q = text[i:i + 3] if text[i:i + 3] in ("'''", '"""') else c
            j, depth = i + len(q), 0
            while j < n:
                if text[j] == "\\" and not raw:
                    j += 2; continue
                if not raw and text[j:j + 2] == "${":
                    depth += 1; j += 2; continue
                if depth and text[j] == "}":
                    depth -= 1; j += 1; continue
                if not depth and text.startswith(q, j) and (len(q) == 3 or text[j] != "\n"):
                    break
                if not depth and len(q) == 1 and text[j] == "\n":
                    break
                j += 1
            end = min(j + len(q), n)
            code.append(text[i:end]); skel.append(q + blank(text[i + len(q):end - len(q)]) + (q if end - len(q) > i else "")); i = end
        else:
            code.append(c); skel.append(c); i += 1
    return "".join(code), "".join(skel)


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


def split_top(m, a, b, sep=","):
    """Index ranges of the non-empty pieces of m[a:b] split on sep at bracket depth 0."""
    out, depth, start = [], 0, a
    for k in range(a, b):
        ch = m[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append((start, k)); start = k + 1
    out.append((start, b))
    return [(x, y) for x, y in out if m[x:y].strip()]


def stmt_end(m, a):
    """Index of the `;` (or a depth-0 cascade `..`) that ends the expression starting at a, or the end of text."""
    depth = 0
    for k in range(a, len(m)):
        if m[k] in "([{":
            depth += 1
        elif m[k] in ")]}":
            if depth == 0:
                return k
            depth -= 1
        elif depth == 0 and (m[k] == ";" or (m.startswith("..", k) and not m.startswith("...", k) and not m.startswith("...", k - 1))):
            return k
    return len(m)


STR = re.compile(r"""\s*(?:r(?P<rq>'[^'\n]*'|"[^"\n]*")|(?P<sq>'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"))""", re.S)


def string_literal_safe(e):
    """True if e is one or more adjacent string literals with no `$` interpolation (a raw string may hold a `$`)."""
    pos, any_ = 0, False
    while pos < len(e):
        m = STR.match(e, pos)
        if not m:
            return False
        body = m.group("sq")
        if body is not None and re.search(r"(?<!\\)\$[A-Za-z_{]", body):
            return False
        pos, any_ = m.end(), True
    return any_


def top_level(sk, op):
    """Index of the first depth-0 occurrence of op in sk (a lone `?` never matches the `??` operator), or -1."""
    depth = 0
    for k in range(len(sk)):
        if sk[k] in "([{":
            depth += 1
        elif sk[k] in ")]}":
            depth -= 1
        elif depth == 0 and sk.startswith(op, k):
            if op == "?" and (sk[k:k + 2] == "??" or (k and sk[k - 1] == "?")):
                continue
            return k
    return -1


def value_ok(e):
    """True if the expression is provably a literal or a header constant."""
    e = re.sub(r"\s+", " ", e.strip().rstrip(","))
    if not e:
        return True
    e = re.sub(r"^const\s+", "", e)
    while e.startswith("(") and e.endswith(")") and match_close(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    sk = lex(e)[1]
    k = top_level(sk, "??")
    if k >= 0:  # a ?? b
        return value_ok(e[:k]) and value_ok(e[k + 2:])
    k = top_level(sk, "?")
    if k >= 0:  # test ? b : c: the test is not a value
        c = top_level(sk[k + 1:], ":")
        return c >= 0 and value_ok(e[k + 1:k + 1 + c]) and value_ok(e[k + 2 + c:])
    if re.fullmatch(r"null|true|false|-?\d+(?:\.\d+)?", e):
        return True
    if string_literal_safe(e):
        return True
    if CONST_NAME.match(e):
        return True
    if e.split(".")[-1] in ascii_names() and re.fullmatch(r"(?:\w+\.)*\w+", e):
        return True
    return any(re.fullmatch(c, e) for c in SAFE_CALLS)


def ok_note(lines, idx):
    if idx < 0 or idx >= len(lines):
        return False
    if HEADER_OK.search(lines[idx]):
        return True
    return idx > 0 and lines[idx - 1].lstrip().startswith("//") and bool(HEADER_OK.search(lines[idx - 1]))


def local_maps(code, skel):
    """Names of file-level const/final maps whose values are all literal (_noStore, _json ...): passing one is safe."""
    names = set()
    for mm in re.finditer(r"\b(?:const|final)\s+(?:[\w<>, ?]+?\s+)?(_?\w+)\s*=\s*(?:const\s+)?(?:<[^{=]*>\s*)?\{", skel):
        ob = mm.end() - 1
        cb = match_close(skel, ob)
        if cb > 0 and not any(True for _ in judge_entries(code, skel, ob + 1, cb, set())):
            names.add(mm.group(1))
    return names


def strip_collection(seg):
    """`if (c) 'k': v` / `else 'k': v` -> `'k': v` (the condition is not a value)."""
    seg = seg.strip()
    for _ in range(3):
        m = re.match(r"(?:else\s+)?(?:if|for)\s*\(", seg)
        if m:
            c = match_close(seg, m.end() - 1)
            seg = seg[c + 1:].strip() if c > 0 else seg
        elif seg.startswith("else "):
            seg = seg[5:].strip()
        else:
            break
    return seg


def passes_through(e, maps):
    """A map handed on, not built here: a name or call with `headers` in it, a local literal map, null or {}."""
    e = re.sub(r"\s+", " ", e.strip())
    e = re.sub(r"\s+as\s+[\w<>?, ]+", "", e)
    e = e.replace("!", "").replace("?", "").strip()
    e = re.sub(r"\?\?\s*(?:const\s+)?(?:<[^{]*>)?\{\s*\}$", "", e).strip()
    while e.startswith("(") and e.endswith(")") and match_close(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    if e in ("", "null", "{}", "const {}") or e in maps:
        return True
    return bool(re.fullmatch(r"[\w.\[\]']*[hH]eaders[\w.\[\]']*(?:\(.*\))?", e))


def judge_entries(code, skel, a, b, maps):
    """(offset, text) of each value in the map-literal body skel[a:b] that is not provably safe."""
    for s_, e_ in split_top(skel, a, b):
        seg_c, seg_k = code[s_:e_], skel[s_:e_]
        lead = len(seg_c) - len(seg_c.lstrip())
        seg = strip_collection(seg_k)
        cut = len(seg_k.strip()) - len(seg)
        base = s_ + lead + cut
        seg_code = code[base:base + len(seg)]
        if seg.startswith("..."):
            if not passes_through(seg_code[3:], maps):
                yield base, seg_code
            continue
        mm = re.match(r"(?:'[^']*'|\"[^\"]*\"|[\w.]+)\s*:\s*", seg)
        v = seg_code[mm.end():] if mm else seg_code
        if not value_ok(v):
            yield base + (mm.end() if mm else 0), v


def judge_map_expr(code, skel, a, b, maps):
    """(offset, text) of the non-literal parts of an expression that should be a header map."""
    ex = skel[a:b]
    lead = len(ex) - len(ex.lstrip())
    a += lead
    ex = ex.strip()
    if not ex:
        return
    if ex.startswith("const "):
        return
    q = top_level(ex, "??")
    if q >= 0:
        yield from judge_map_expr(code, skel, a, a + q, maps)
        yield from judge_map_expr(code, skel, a + q + 2, a + len(ex), maps)
        return
    q = top_level(ex, "?")
    if q >= 0:
        c = top_level(ex[q + 1:], ":")
        if c >= 0:
            yield from judge_map_expr(code, skel, a + q + 1, a + q + 1 + c, maps)
            yield from judge_map_expr(code, skel, a + q + 2 + c, a + len(ex), maps)
            return
    ob = re.match(r"(?:<[^{}()]*>\s*)?\{", ex)
    if ob:
        o = a + ob.end() - 1
        c = match_close(skel, o)
        yield from judge_entries(code, skel, o + 1, c, maps)
        return
    if not passes_through(code[a:a + len(ex)], maps):
        yield a, code[a:a + len(ex)]


def header_writes(code, skel):
    """[(offset, kind, value_text)] of every place a request header gets a value that is not provably safe in this file."""
    found, maps = [], local_maps(code, skel)
    # headers[k] = v   /   headers[k] ??= v
    for mm in re.finditer(r"(?<![\w.])(?:[\w.]+\.)?\w*[hH]eaders\s*\[", skel):
        o = mm.end() - 1
        c = match_close(skel, o)
        if c < 0:
            continue
        am = re.match(r"\s*(\?\?=|=)(?!=)", skel[c + 1:])
        if am:
            a = c + 1 + am.end()
            v = code[a:stmt_end(skel, a)]
            if not value_ok(v):
                found.append((mm.start(), "headers[]=", v))
    # headers.set / add / putIfAbsent / addAll / update
    for mm in re.finditer(r"(?<![\w.])(?:[\w.]+\.)?\w*[hH]eaders\s*\.\s*(set|add|putIfAbsent|addAll|update)\s*\(", skel):
        o = mm.end() - 1
        c = match_close(skel, o)
        if c < 0:
            continue
        parts = split_top(skel, o + 1, c)
        kind = mm.group(1)
        if kind in ("set", "add") and len(parts) >= 2:
            v = code[parts[1][0]:parts[1][1]]
            if not value_ok(v):
                found.append((mm.start(), "headers." + kind, v))
        elif kind in ("putIfAbsent", "update") and len(parts) >= 2:
            body = code[parts[1][0]:parts[1][1]]
            ar = re.match(r"\s*\(\s*\)\s*=>\s*", body)
            v = body[ar.end():] if ar else body
            if not value_ok(v):
                found.append((mm.start(), "headers." + kind, v))
        elif kind == "addAll" and parts:
            for off, v in judge_map_expr(code, skel, parts[0][0], parts[0][1], maps):
                found.append((off, "headers.addAll", v))
    # headers: <map or expr>  (named argument)
    for mm in re.finditer(r"(?<![\w.])headers\s*:\s*", skel):
        s = mm.end()
        e = s
        depth = 0
        ta = re.match(r"(?:const\s+)?<[^{}()]*>\s*", skel[s:])  # `<String, String>{...}`: the comma in the type is not an argument break
        for k in range(s + (ta.end() if ta else 0), len(skel)):
            if skel[k] in "([{":
                depth += 1
            elif skel[k] in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif skel[k] == "," and depth == 0:
                break
            e = k + 1
        for off, v in judge_map_expr(code, skel, s, e, maps):
            found.append((off, "headers:", v))
    # a header map BUILT here: `final h = {..}` / `fooHeaders = {..}` and `Map<..> fooHeaders(..) => {..}` / `{ return {..}; }`
    for mm in re.finditer(r"\b(\w*[hH]eaders\w*)\s*=\s*(?!=)", skel):
        a = mm.end()
        if re.match(r"(?:const\s+)?(?:<[^{}()]*>\s*)?\{", skel[a:]):
            for off, v in judge_map_expr(code, skel, a, stmt_end(skel, a), maps):
                found.append((off, "headers-map=", v))
    for mm in re.finditer(r"(?:^|\n)[ \t]*(?:static\s+)?(?:Map<[^;{(=]*>|\w+)\??\s+(\w*[hH]eaders\w*)\s*\(", skel):
        cp = match_close(skel, mm.end() - 1)
        if cp < 0:
            continue
        rest = skel[cp + 1:]
        lead = len(rest) - len(rest.lstrip())
        if rest.lstrip().startswith("=>"):
            a = cp + 1 + lead + 2
            for off, v in judge_map_expr(code, skel, a, stmt_end(skel, a), maps):
                found.append((off, "headers-fn", v))
        elif rest.lstrip().startswith("{"):
            ob = cp + 1 + lead
            body_end = match_close(skel, ob)
            locals_ = set()
            for rm in re.finditer(r"\breturn\b", skel[ob:body_end]):
                a = ob + rm.end()
                rv = skel[a:stmt_end(skel, a)].strip()
                if re.fullmatch(r"[A-Za-z_]\w*", rv) and "eaders" not in rv:
                    locals_.add(rv)  # `return h;`: judged by what is written into h, below
                    continue
                for off, v in judge_map_expr(code, skel, a, stmt_end(skel, a), maps):
                    found.append((off, "headers-fn", v))
            for name in locals_:
                for wm in re.finditer(r"(?<![\w.])%s\s*\[" % re.escape(name), skel[ob:body_end]):
                    o2 = ob + wm.end() - 1
                    c2 = match_close(skel, o2)
                    am = re.match(r"\s*(\?\?=|=)(?!=)", skel[c2 + 1:]) if c2 > 0 else None
                    if am:
                        a2 = c2 + 1 + am.end()
                        v = code[a2:stmt_end(skel, a2)]
                        if not value_ok(v):
                            found.append((ob + wm.start(), "headers-fn", v))
                for wm in re.finditer(r"\b(?:final|var)\s+(?:[\w<>, ?]+?\s+)?%s\s*=\s*(?:const\s+)?(?:<[^{}()]*>\s*)?\{" % re.escape(name), skel[ob:body_end]):
                    o2 = ob + wm.end() - 1
                    for off, v in judge_entries(code, skel, o2 + 1, match_close(skel, o2), maps):
                        found.append((off, "headers-fn", v))
    return found


def scan_text(path, text, added=None, repos=(), exempt=DEFAULT_EXEMPT):
    """[(line, kind, snippet)] for the non-literal header writes this file adds (added: 1-based line numbers, None = all)."""
    for ent in filter(None, (x.strip() for x in exempt.split(","))):
        repo, _, prefix = ent.partition(":")
        if repo in repos and path.startswith(prefix):
            return []
    code, skel = lex(text)
    nl = [i for i, ch in enumerate(text) if ch == "\n"]
    lines = text.splitlines()
    out = []
    for off, kind, value in header_writes(code, skel):
        line = bisect.bisect_left(nl, off + (len(code[off:]) - len(code[off:].lstrip()))) + 1
        if added is not None and line not in added:
            continue
        if ok_note(lines, line - 1):
            continue
        out.append((line, kind, " ".join(value.split())[:80]))
    return sorted(set(out))


def guard_body(code, skel, d):
    """The text of the guard function (parameters and body) that the regex match d opened."""
    cp = match_close(skel, d.end() - 1)
    if cp < 0:
        return code[d.start():]
    rest = skel[cp + 1:]
    lead = len(rest) - len(rest.lstrip())
    if rest.lstrip().startswith("{"):
        ob = cp + 1 + lead
        return code[d.start():match_close(skel, ob) + 1]
    return code[d.start():stmt_end(skel, cp + 1)]


def pins_problems(read):
    """Rule B: problems with the guard pins. read(path) -> text or None."""
    probs = []
    p = PINS
    text = read(p["file"])
    if text is None:
        probs.append("pinned file %s is missing: if it moved, update PINS in purpusgit/.github scripts/http_headers.py" % p["file"])
    else:
        code, skel = lex(text)
        d = re.search(r"bool\s+%s\s*\(" % re.escape(p["guard"]), skel)
        if not d:
            probs.append("%s: the Latin-1 guard `bool %s(` is gone. It must stay (client_core#1060)" % (p["file"], p["guard"]))
        elif not re.search(r"0x[fF]{2}\b|\b255\b|latin1", guard_body(code, skel, d), re.I):
            probs.append("%s: %s no longer tests for 0xFF (ISO-8859-1)" % (p["file"], p["guard"]))
        cl = code.splitlines()
        writes = [i for i, ln in enumerate(cl) if p["header"] in ln and re.search(r"\]\s*=(?!=)", ln)]
        if not writes:
            probs.append("%s: no write of `%s` found; if it moved, update PINS" % (p["file"], p["header"]))
        for i in writes:
            if not any(p["guard"] + "(" in cl[j] for j in (i, i - 1) if j >= 0):
                probs.append("%s:%d: `%s` is written without %s(...). Free text must pass the Latin-1 guard" % (p["file"], i + 1, p["header"], p["guard"]))
    for f in p["interceptors"]:
        t = read(f)
        if t is None:
            probs.append("pinned interceptor %s is missing: if it moved, update PINS" % f)
        elif p["applier"] not in lex(t)[1]:
            probs.append("%s no longer calls %s: the org headers (and their guard) would be bypassed" % (f, p["applier"]))
    return probs


def added_lines(base):
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


def gate(base, root="."):
    repos = repo_names(root)
    try:
        git("rev-parse", "--verify", "-q", base + "^{commit}")
        files = added_lines(base)
    except subprocess.CalledProcessError as e:
        print("::error::http-headers could not diff against " + base + ": " + (e.stderr or "").strip() + ". This is a gate failure, not a clean tree.")
        return 2
    exempt = os.environ.get("HTTP_HEADERS_EXEMPT") or DEFAULT_EXEMPT
    findings = []
    for path, lines in sorted(files.items()):
        try:
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        findings += [(path, *f) for f in scan_text(path, text, lines, repos, exempt)]
    probs, missing = [], False
    if CORE & set(repos):
        def read(p):
            try:
                with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except OSError:
                return None
        probs = pins_problems(read)
        missing = any("is missing" in x for x in probs)
    print("http-headers: checked the lines added in %d lib/ file(s) against %s%s." % (len(files), base, "; client_core guard pins checked" if CORE & set(repos) else ""))
    for path, n, kind, snip in findings:
        print("::error file=%s,line=%d::HTTP-HEADERS: a header set to a non-literal value (%s): %s" % (path, n, kind, snip))
        print("%s:%d: %s = %s" % (path, n, kind, snip))
    for pr in probs:
        print("::error::HTTP-HEADERS pin: " + pr)
    if findings:
        print("\n" + str(len(findings)) + " header write(s) with a value that is not provably ISO-8859-1. " + FIX_A)
    if probs or findings:
        return 2 if (missing and not findings) else 1
    print("http-headers passed.")
    return 0


if __name__ == "__main__":
    sys.exit(gate(os.environ.get("BASE_REF") or "origin/cwb"))
