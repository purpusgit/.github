#!/usr/bin/env python3
"""taxo_lint.py — Taxonomy CI/lint gate.

Prevents the recurring "stale-type / wrong-column / stale-FK-ref" bug class in
taxonomy-touching code and data. Three independent modes:

  --data              Validate live taxo.* data (needs a DB connection).
                      Primary gate: every onboarding_question FK:<Type> ref must
                      point at a taxo.master type that still has >0 active leaves.

  --code <dir>        Static regex scan (no DB). Catches:
                        G1  PascalCase type literals on taxo.master queries
                            (Rule 59 — new taxonomy names are snake_case only).
                        G2  taxo.master queries referencing a column that is not
                            in the real taxo.master schema allowlist
                            (parent_id / parent_idfr / joining_label et al.).

  --code-db <dir>     Same static scan as --code PLUS a live-DB check (G3).

SCAN SCOPE (2026-07-31 broadening — Fable decisions 1 + 2a):
  * `**/*.sql.ts`, `**/*.sql.v[0-9]*.ts`, and `**/*.sql` (migrations) and other
    `**/*.ts` are all scanned. `backups/`, `dumps/`, `__tests__/`, `*.spec.ts`,
    `*.test.ts` are excluded.
  * `.sql` files (pure SQL): statement-window scan — the whole file is SQL, so a
    taxo.master mention scopes the surrounding statement. 0 FP on migrations.
  * `.ts` files (inline SQL in service/route/*.sql.ts): DEFAULT-DENY template
    scoping — a type/column hit counts ONLY if it sits inside the same backtick
    template literal that contains the `taxo.master` mention, with `${...}`
    interpolations blanked. This structurally kills the false-positive class where
    an unrelated TS `type = 'X_Y'` sits near a bare `'taxo.master'` string (e.g. a
    test), rather than relying on a line-proximity window (the same defect family
    as taxo-contract checker.py 2.2's _nearest_idfr, fixed 2026-07-31).
    Before template parsing, `_mask_ts_noncode` blanks JS comments and ordinary
    '/" strings (offset-preserving), so a stray backtick in a comment or string
    cannot desync backtick-parity and blind the whole file.
    NOTE: SQL that is NOT inside a backtick template (single-quote concatenation)
    is not scanned in .ts files — the org convention is that taxo SQL lives in
    backtick template literals / *.sql.ts files.

Exit code is 0 when clean, non-zero when any check fails.

DB credentials for --data / --code-db come from (in order):
  1. --db-json <path> + --db-env <name>   (bc_mysql_envs.json style file)
  2. env vars TAXO_DB_HOST / TAXO_DB_PORT / TAXO_DB_USER / TAXO_DB_PASSWORD
"""
import argparse
import glob
import os
import re
import sys


TAXO_MASTER_COLUMNS = {
    "id", "identifier", "code", "short_name", "type", "value", "description",
    "hierarchy_level", "realm_idfr", "domain_idfr", "category_idfr",
    "display_order", "keywords", "phase", "source_file", "is_active",
    "is_deleted", "created_at", "updated_at", "glyph",
}
KNOWN_BAD_COLUMNS = {"parent_id", "parent_idfr", "joining_label"}


# ===========================================================================
# DATA MODE  (unchanged)
# ===========================================================================
def _load_db_creds(args):
    if args.db_json:
        import json
        with open(args.db_json) as fh:
            env = json.load(fh)[args.db_env]
        return dict(host=env["host"], port=int(env.get("port", 3306)),
                    user=env["user"], password=env["password"])
    host = os.environ.get("TAXO_DB_HOST")
    if not host:
        sys.exit("FATAL: no DB credentials — pass --db-json/--db-env or set "
                 "TAXO_DB_HOST/PORT/USER/PASSWORD env vars.")
    return dict(host=host, port=int(os.environ.get("TAXO_DB_PORT", "3306")),
                user=os.environ.get("TAXO_DB_USER", ""),
                password=os.environ.get("TAXO_DB_PASSWORD", ""))


def run_data(args):
    import pymysql
    creds = _load_db_creds(args)
    conn = pymysql.connect(connect_timeout=20, **creds)
    cur = conn.cursor()
    failures = []
    cur.execute(
        "SELECT id, answer_options_ref FROM taxo.onboarding_question "
        "WHERE answer_options_ref LIKE 'FK:%' AND is_deleted = 0 ORDER BY id")
    fk_rows = cur.fetchall()
    stale_types = set()
    for qid, ref in fk_rows:
        target = ref.split("FK:", 1)[1].strip()
        cur.execute(
            "SELECT COUNT(*) FROM taxo.master WHERE type = %s "
            "AND hierarchy_level = 'leaf' AND is_active = 1 AND is_deleted = 0",
            (target,))
        n = cur.fetchone()[0]
        if n == 0:
            failures.append((qid, ref, target, 0))
            stale_types.add(target)
    missing_types = []
    if args.assert_type:
        for t in args.assert_type:
            cur.execute("SELECT COUNT(*) FROM taxo.master WHERE type = %s "
                        "AND is_active = 1 AND is_deleted = 0", (t,))
            if cur.fetchone()[0] == 0:
                missing_types.append(t)
    conn.close()
    print("=" * 72)
    print("taxo_lint --data : taxo.* data validation")
    print("=" * 72)
    print(f"[Check 1] onboarding_question FK refs scanned : {len(fk_rows)}")
    print(f"[Check 1] stale FK rows (target 0 leaves)     : {len(failures)}")
    print(f"[Check 1] distinct stale target types         : {len(stale_types)}")
    if failures:
        print()
        print(f"{'q.id':>6}  {'target type':<42}  {'leaves':>6}")
        print(f"{'-'*6}  {'-'*42}  {'-'*6}")
        for qid, ref, target, n in failures:
            print(f"{qid:>6}  {target:<42}  {n:>6}")
    if args.assert_type:
        print()
        print(f"[Check 2] asserted type strings               : {len(args.assert_type)}")
        print(f"[Check 2] missing (0 rows)                     : {len(missing_types)}")
        for t in missing_types:
            print(f"          MISSING: {t}")
    failed = bool(failures) or bool(missing_types)
    print()
    if failed:
        print("RESULT: FAIL — taxonomy data has stale references.")
        return 1
    print("RESULT: PASS — every FK ref resolves to a live taxo.master type.")
    return 0


# ===========================================================================
# CODE MODE
# ===========================================================================
_PASCAL_TYPE = re.compile(r"type\s*=\s*'([A-Z][A-Za-z0-9]*_[A-Za-z0-9_]*)'")
_LOWER_TYPE = re.compile(r"type\s*=\s*'([a-z][a-z0-9_]*)'")
_SQL_NON_ALIAS = {
    "as", "on", "where", "join", "left", "right", "inner", "outer", "cross",
    "full", "group", "order", "limit", "offset", "set", "using", "and", "or",
    "union", "having", "select", "from", "natural", "for", "into", "values",
    "lock", "straight_join",
}
_TAXO_MASTER_ALIAS = re.compile(
    r"\btaxo\.master\b(?:\s+(?:as\s+)?([A-Za-z_]\w*))?", re.IGNORECASE)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_$][\w.$]*)", re.IGNORECASE)
_BAD_COL = re.compile(
    r"(?<![\w.$])((?:[A-Za-z_$]\w*\s*\.\s*)*)"
    r"(" + "|".join(sorted(KNOWN_BAD_COLUMNS, key=len, reverse=True)) + r")\b")
_TAXO = re.compile(r"\btaxo\.master\b", re.IGNORECASE)

GLOB_PATTERNS = ("*.sql.ts", "*.sql.v[0-9]*.ts", "*.sql", "*.ts")


def _excluded(rel):
    """Exclude mysqldumps/backups (volume + seeded literals) and test files
    (a helper named fixtures.ts walks past filename rules, so exclude the whole
    __tests__ tree too)."""
    rel = rel.replace(os.sep, "/")
    parts = rel.split("/")
    if "backups" in parts or "dumps" in parts or "__tests__" in parts:
        return True
    base = parts[-1]
    return base.endswith(".spec.ts") or base.endswith(".test.ts")


def _strip_line_comments(text):
    out = []
    for line in text.split("\n"):
        line = re.sub(r"--.*$", "", line)
        line = re.sub(r"//.*$", "", line)
        out.append(line)
    return "\n".join(out)


def _line_at(text, off):
    return text.count("\n", 0, off) + 1


def _mask_ts_noncode(text):
    """Offset-preserving mask of JS line/block comments and ordinary '/" string
    literals, so a stray backtick inside them cannot desync backtick-parity in
    _template_spans (a single odd backtick in a comment/string otherwise blinds the
    whole file — the vacuous-coverage class this gate exists to close).

    Backtick TEMPLATE regions are left INTACT — their SQL single-quoted literals
    (e.g. type = 'Org_Department') are the data we detect. `${...}` interpolations
    are entered as code, so their comments/strings get masked too. Newlines are
    preserved so line numbers stay valid.

    Known limitation: JS regex literals are not tracked (division vs regex is
    context-dependent); a backtick inside a regex could still desync. Rare in SQL
    modules; documented rather than solved.
    """
    out = list(text)
    n = len(text)
    i = 0
    # frame stack: ["code", brace_depth, is_interp] | ["tmpl"]
    stack = [["code", 0, False]]

    def bl(k):
        if out[k] != "\n":
            out[k] = " "

    while i < n:
        top = stack[-1]
        c = text[i]
        if top[0] == "code":
            nxt = text[i + 1] if i + 1 < n else ""
            if c == "/" and nxt == "/":
                while i < n and text[i] != "\n":
                    bl(i)
                    i += 1
                continue
            if c == "/" and nxt == "*":
                bl(i)
                bl(i + 1)
                i += 2
                while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                    bl(i)
                    i += 1
                if i < n:
                    bl(i)
                    if i + 1 < n:
                        bl(i + 1)
                    i += 2
                continue
            if c == '"' or c == "'":
                q = c
                i += 1
                while i < n and text[i] != q:
                    if text[i] == "\\":
                        bl(i)
                        if i + 1 < n:
                            bl(i + 1)
                        i += 2
                        continue
                    bl(i)
                    i += 1
                i += 1  # keep the closing quote (it is not a backtick)
                continue
            if c == "`":
                stack.append(["tmpl"])
                i += 1
                continue
            if c == "{":
                top[1] += 1
                i += 1
                continue
            if c == "}":
                if top[2] and top[1] == 0:
                    stack.pop()  # close ${...} interpolation
                elif top[1] > 0:
                    top[1] -= 1
                i += 1
                continue
            i += 1
        else:  # inside a backtick template — preserve
            if c == "\\":
                i += 2
                continue
            if c == "`":
                stack.pop()
                i += 1
                continue
            if c == "$" and i + 1 < n and text[i + 1] == "{":
                stack.append(["code", 0, True])
                i += 2
                continue
            i += 1
    return "".join(out)


# ---- .sql (whole-file SQL) : statement-window scan -------------------------
def _taxo_master_line_windows(lines):
    idx = set()
    for i, ln in enumerate(lines):
        if _TAXO.search(ln):
            idx.add(i)
            for j in range(i + 1, min(i + 45, len(lines))):
                idx.add(j)
                if lines[j].rstrip().endswith(";") or lines[j].strip() == "`":
                    break
            for j in range(max(0, i - 30), i):
                idx.add(j)
    return idx


def _taxo_master_blocks(lines):
    blocks = []
    for i, ln in enumerate(lines):
        if not _TAXO.search(ln):
            continue
        start = max(0, i - 30)
        end = i
        for j in range(i + 1, min(i + 45, len(lines))):
            end = j
            if lines[j].rstrip().endswith(";") or lines[j].strip() == "`":
                break
        text = "\n".join(lines[start:end + 1])
        aliases = {m.group(1) for m in _TAXO_MASTER_ALIAS.finditer(text)
                   if m.group(1) and m.group(1).lower() not in _SQL_NON_ALIAS}
        has_other = any(m.group(1).lower() != "taxo.master"
                        for m in _TABLE_REF.finditer(text))
        blocks.append((start, end, aliases, has_other))
    return blocks


def _bad_col_is_taxo_ref(prefix, aliases, has_other):
    quals = [q.strip() for q in prefix.split(".") if q.strip()]
    if not quals:
        return not has_other
    if [q.lower() for q in quals][-2:] == ["taxo", "master"]:
        return True
    return quals[-1] in aliases


def _scan_sql_text(path, text, g1_hits, g2_set, lower_hits):
    lines = text.split("\n")
    windows = _taxo_master_line_windows(lines)
    for i, ln in enumerate(lines):
        if i not in windows:
            continue
        for m in _PASCAL_TYPE.finditer(ln):
            g1_hits.append((path, i + 1, m.group(1)))
        for m in _LOWER_TYPE.finditer(ln):
            lower_hits.append((path, i + 1, m.group(1)))
    for start, end, aliases, has_other in _taxo_master_blocks(lines):
        for i in range(start, end + 1):
            ln = lines[i]
            for m in _BAD_COL.finditer(ln):
                if re.search(r"\bas\s*$", ln[:m.start()], re.IGNORECASE):
                    continue
                if _bad_col_is_taxo_ref(m.group(1), aliases, has_other):
                    g2_set.add((path, i + 1, m.group(2)))


# ---- .ts (inline SQL) : default-deny template scoping ----------------------
def _skip_quote(text, i, q):
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == q:
            return j + 1
        j += 1
    return n


def _skip_nested_template(text, i):
    n = len(text)
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1
        if c == "$" and j + 1 < n and text[j + 1] == "{":
            j += 2
            depth = 1
            while j < n and depth:
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "{":
                    depth += 1
                elif d == "}":
                    depth -= 1
                elif d == "`":
                    j = _skip_nested_template(text, j)
                    continue
                j += 1
            continue
        j += 1
    return n


def _template_spans(text):
    """(start, end, interps) for each top-level backtick template: content offsets
    plus the `${...}` ranges to blank. Handles nested templates, quotes inside
    interpolations, and escaped backticks."""
    spans, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "`":
            i += 1
            continue
        start = i + 1
        j = start
        interps = []
        while j < n:
            c = text[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                break
            if c == "$" and j + 1 < n and text[j + 1] == "{":
                a = j
                j += 2
                depth = 1
                while j < n and depth:
                    d = text[j]
                    if d == "\\":
                        j += 2
                        continue
                    if d == "{":
                        depth += 1
                    elif d == "}":
                        depth -= 1
                    elif d == "`":
                        j = _skip_nested_template(text, j)
                        continue
                    elif d in "\"'":
                        j = _skip_quote(text, j, d)
                        continue
                    j += 1
                interps.append((a, j))
                continue
            j += 1
        spans.append((start, j, interps))
        i = j + 1
    return spans


def _blank_interps(text, s, e, interps):
    seg = list(text[s:e])
    for a, b in interps:
        for k in range(a, b):
            if text[k] != "\n":
                seg[k - s] = " "
    return "".join(seg)


def _scan_ts_text(path, text, g1_hits, g2_set, lower_hits):
    for s, e, interps in _template_spans(text):
        seg = _blank_interps(text, s, e, interps)
        if not _TAXO.search(seg):
            continue
        aliases = {m.group(1) for m in _TAXO_MASTER_ALIAS.finditer(seg)
                   if m.group(1) and m.group(1).lower() not in _SQL_NON_ALIAS}
        has_other = any(m.group(1).lower() != "taxo.master"
                        for m in _TABLE_REF.finditer(seg))
        for m in _PASCAL_TYPE.finditer(seg):
            g1_hits.append((path, _line_at(text, s + m.start()), m.group(1)))
        for m in _LOWER_TYPE.finditer(seg):
            lower_hits.append((path, _line_at(text, s + m.start()), m.group(1)))
        for m in _BAD_COL.finditer(seg):
            if re.search(r"\bas\s*$", seg[:m.start()], re.IGNORECASE):
                continue
            if _bad_col_is_taxo_ref(m.group(1), aliases, has_other):
                g2_set.add((path, _line_at(text, s + m.start()), m.group(2)))


def _scan_files(root):
    """Static scan. Returns (files, g1_hits, g2_hits, lower_hits) — same shape as
    before, so run_code / run_code_db are unchanged."""
    files = sorted({
        f
        for pat in GLOB_PATTERNS
        for f in glob.glob(os.path.join(root, "**", pat), recursive=True)
        if os.path.isfile(f) and not _excluded(os.path.relpath(f, root))
    })
    g1_hits, g2_set, lower_hits = [], set(), []
    for path in files:
        try:
            raw = open(path, encoding="utf-8", errors="replace").read()
        except Exception as e:  # pragma: no cover
            print(f"Warning: could not read {path}: {e}")
            continue
        if not _TAXO.search(raw):
            continue
        if path.endswith(".sql"):
            # pure SQL — strip -- / // line comments, whole file is SQL
            _scan_sql_text(path, _strip_line_comments(raw), g1_hits, g2_set, lower_hits)
        else:
            # .ts — mask JS comments + code strings (offset-preserving) so stray
            # backticks in them cannot desync template parity; templates preserved
            _scan_ts_text(path, _mask_ts_noncode(raw), g1_hits, g2_set, lower_hits)
    return files, g1_hits, sorted(g2_set), lower_hits


def _print_g1_g2(g1_hits, g2_hits):
    if g1_hits:
        print("\n-- G1 violations (use snake_case lowercase types) --")
        for path, ln, val in g1_hits:
            print(f"  {path}:{ln}: type = '{val}'  ->  should be lowercase snake_case")
    if g2_hits:
        print("\n-- G2 violations (column not in taxo.master schema) --")
        for path, ln, col in g2_hits:
            print(f"  {path}:{ln}: '{col}' is not a taxo.master column")


def run_code(args):
    files, g1_hits, g2_hits, _ = _scan_files(args.dir)
    print("=" * 72)
    print("taxo_lint --code : static scan")
    print("=" * 72)
    print(f"scanned files                      : {len(files)}")
    print(f"[G1] PascalCase taxo type literals : {len(g1_hits)}  (Rule 59)")
    print(f"[G2] wrong taxo.master columns     : {len(g2_hits)}")
    _print_g1_g2(g1_hits, g2_hits)
    failed = bool(g1_hits) or bool(g2_hits)
    print()
    if failed:
        print("RESULT: FAIL — taxonomy code references are stale. Fix before merge.")
        return 1
    print("RESULT: PASS — no stale taxo type literals or column references.")
    return 0


def run_code_db(args):
    import pymysql
    files, g1_hits, g2_hits, lower_hits = _scan_files(args.dir)
    creds = _load_db_creds(args)
    conn = pymysql.connect(connect_timeout=20, **creds)
    cur = conn.cursor()
    distinct = sorted({t for _, _, t in lower_hits})
    missing = set()
    for t in distinct:
        cur.execute("SELECT COUNT(*) FROM taxo.master WHERE type = %s AND is_deleted = 0", (t,))
        if cur.fetchone()[0] == 0:
            missing.add(t)
    conn.close()
    g3_hits = [(p, ln, t) for (p, ln, t) in lower_hits if t in missing]
    print("=" * 72)
    print("taxo_lint --code-db : static scan + live-DB type existence")
    print("=" * 72)
    print(f"scanned files                           : {len(files)}")
    print(f"[G1] PascalCase taxo type literals      : {len(g1_hits)}  (Rule 59)")
    print(f"[G2] wrong taxo.master columns          : {len(g2_hits)}")
    print(f"[G3] lowercase types checked (live DB)  : {len(distinct)}")
    print(f"[G3] lowercase types absent from master : {len(missing)}")
    _print_g1_g2(g1_hits, g2_hits)
    if g3_hits:
        print("\n-- G3 violations (lowercase type not found in live taxo.master) --")
        for path, ln, t in g3_hits:
            print(f"  {path}:{ln}: type = '{t}'  ->  no such type in taxo.master")
    failed = bool(g1_hits) or bool(g2_hits) or bool(g3_hits)
    print()
    if failed:
        print("RESULT: FAIL — taxonomy code references are stale. Fix before merge.")
        return 1
    print("RESULT: PASS — no stale taxo type literals or column references.")
    return 0


# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description="Taxonomy CI/lint gate.")
    sub = ap.add_mutually_exclusive_group(required=True)
    sub.add_argument("--data", action="store_true",
                     help="Validate live taxo.* data (needs DB).")
    sub.add_argument("--code", metavar="DIR",
                     help="Static scan under DIR (no DB).")
    sub.add_argument("--code-db", metavar="DIR",
                     help="Static scan under DIR PLUS a live-DB existence check (G3).")
    ap.add_argument("--db-json", help="Path to bc_mysql_envs.json style creds file.")
    ap.add_argument("--db-env", default="sandbox", help="Env key inside --db-json.")
    ap.add_argument("--assert-type", action="append", default=[],
                    help="(--data) Assert this taxo.master type has >0 rows. Repeatable.")
    args = ap.parse_args()
    if args.code:
        args.dir = args.code
        sys.exit(run_code(args))
    if args.code_db:
        args.dir = args.code_db
        sys.exit(run_code_db(args))
    sys.exit(run_data(args))


if __name__ == "__main__":
    main()
