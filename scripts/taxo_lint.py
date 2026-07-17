#!/usr/bin/env python3
"""taxo_lint.py — Taxonomy CI/lint gate.

Prevents the recurring "stale-type / wrong-column / stale-FK-ref" bug class in
taxonomy-touching code and data. Three independent modes:

  --data              Validate live taxo.* data (needs a DB connection).
                      Primary gate: every onboarding_question FK:<Type> ref must
                      point at a taxo.master type that still has >0 active leaves.

  --code <dir>        Static regex scan of *.sql.ts files (no DB). Catches:
                        G1  PascalCase type literals on taxo.master queries
                            (Rule 59 — new taxonomy names are snake_case only).
                        G2  taxo.master queries referencing a column that is not
                            in the real taxo.master schema allowlist
                            (parent_id / parent_idfr / joining_label et al.).

  --code-db <dir>     Same static scan as --code PLUS a live-DB check (needs a
                      DB connection). Closes the last gap G1 cannot: a
                      *lowercase-but-nonexistent* taxo type (e.g. type =
                      'wrong_type_here') passes G1's PascalCase test yet points
                      at nothing. G3 extracts every lowercase `type = '<...>'`
                      literal on a taxo.master query and asserts each one still
                      exists in live taxo.master (SELECT COUNT(*) > 0). G1/G2
                      run unchanged alongside it.

Exit code is 0 when clean, non-zero when any check fails, so it drops straight
into a CI step. Failures print as a clean table with file:line or ref detail.

DB credentials for --data / --code-db come from (in order):
  1. --db-json <path> + --db-env <name>   (bc_mysql_envs.json style file)
  2. env vars TAXO_DB_HOST / TAXO_DB_PORT / TAXO_DB_USER / TAXO_DB_PASSWORD
     (CI wires these from repo/org secrets — see reusable-taxo-lint.yml and
     taxo-data-lint-nightly.yml).

Author note (Rule 39 overlap check): grepped the Rule 13 script index + the
service repos — no existing script performs the onboarding FK:<Type> ->
taxo.master leaf-coverage assertion or the .sql.ts PascalCase/column scan.
This is a new, single-purpose CI checker; it writes nothing to any DB.
"""
import argparse
import glob
import os
import re
import sys


# ---------------------------------------------------------------------------
# Real taxo.master columns. Anything else referenced in a taxo.master query is
# a stale/wrong-column reference (the G2 bug class). Keep this list in sync with
# `DESCRIBE taxo.master`.
# ---------------------------------------------------------------------------
TAXO_MASTER_COLUMNS = {
    "id", "identifier", "code", "short_name", "type", "value", "description",
    "hierarchy_level", "realm_idfr", "domain_idfr", "category_idfr",
    "display_order", "keywords", "phase", "source_file", "is_active",
    "is_deleted", "created_at", "updated_at", "glyph",
}

# Columns that are known-bad and must always be flagged when they appear against
# taxo.master, even if a future edit to the allowlist slips.
KNOWN_BAD_COLUMNS = {"parent_id", "parent_idfr", "joining_label"}


# ===========================================================================
# DATA MODE
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
    failures = []  # (question_id, ref, target_type, leaf_count)

    # --- Check 1 (REQUIRED): onboarding FK:<Type> -> taxo.master leaf coverage
    cur.execute(
        "SELECT id, answer_options_ref FROM taxo.onboarding_question "
        "WHERE answer_options_ref LIKE 'FK:%' AND is_deleted = 0 "
        "ORDER BY id"
    )
    fk_rows = cur.fetchall()
    stale_types = set()
    for qid, ref in fk_rows:
        target = ref.split("FK:", 1)[1].strip()
        cur.execute(
            "SELECT COUNT(*) FROM taxo.master WHERE type = %s "
            "AND hierarchy_level = 'leaf' AND is_active = 1 AND is_deleted = 0",
            (target,),
        )
        n = cur.fetchone()[0]
        if n == 0:
            failures.append((qid, ref, target, 0))
            stale_types.add(target)

    # --- Check 2 (OPTIONAL hook): assert given type strings have >0 leaves
    missing_types = []
    if args.assert_type:
        for t in args.assert_type:
            cur.execute(
                "SELECT COUNT(*) FROM taxo.master WHERE type = %s "
                "AND is_active = 1 AND is_deleted = 0", (t,))
            if cur.fetchone()[0] == 0:
                missing_types.append(t)
    conn.close()

    # --- Report
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
        print("RESULT: FAIL — taxonomy data has stale references. "
              "Repoint the FK refs (or add the taxo.master leaves) before merge.")
        return 1
    print("RESULT: PASS — every FK ref resolves to a live taxo.master type.")
    return 0


# ===========================================================================
# CODE MODE
# ===========================================================================
# A taxo.master query "window": we consider a line taxo.master-relevant if the
# file references taxo.master AND the line is within a SQL context. To keep the
# scan simple and low-false-positive, G1/G2 fire on lines that mention
# taxo.master directly OR appear in a file that queries taxo.master. We scope
# per-statement using a sliding window around each `taxo.master` mention.

_PASCAL_TYPE = re.compile(r"type\s*=\s*'([A-Z][A-Za-z0-9]*_[A-Za-z0-9_]*)'")

# Lowercase snake_case type literal on a taxo.master query. G1 (PascalCase)
# cannot see these — a valid-LOOKING but non-existent lowercase type slips the
# static scan entirely. G3 (--code-db) resolves each one against the live DB.
_LOWER_TYPE = re.compile(r"type\s*=\s*'([a-z][a-z0-9_]*)'")

# Keywords that can immediately follow `taxo.master` but are NOT a table alias.
_SQL_NON_ALIAS = {
    "as", "on", "where", "join", "left", "right", "inner", "outer", "cross",
    "full", "group", "order", "limit", "offset", "set", "using", "and", "or",
    "union", "having", "select", "from", "natural", "for", "into", "values",
    "lock", "straight_join",
}

# `taxo.master`, optionally followed by `AS <alias>` or a bare `<alias>`.
_TAXO_MASTER_ALIAS = re.compile(
    r"\btaxo\.master\b(?:\s+(?:as\s+)?([A-Za-z_]\w*))?", re.IGNORECASE)
# A table reference introduced by FROM / JOIN.
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_$][\w.$]*)", re.IGNORECASE)
# A known-bad column with its (possibly dotted, possibly empty) qualifier path:
#   sub_org.parent_id     -> prefix "sub_org.",     col "parent_id"
#   taxo.master.parent_id -> prefix "taxo.master.", col "parent_id"
#   parent_id             -> prefix "",             col "parent_id"
# The lookbehind stops the match starting mid-identifier (org_parent_id) or
# mid-path. Longer bad names first so parent_idfr wins over parent_id.
_BAD_COL = re.compile(
    r"(?<![\w.$])((?:[A-Za-z_$]\w*\s*\.\s*)*)"
    r"(" + "|".join(sorted(KNOWN_BAD_COLUMNS, key=len, reverse=True)) + r")\b"
)


def _strip_line_comments(text):
    # Remove // and -- line comments so literals inside comments don't trip us.
    out = []
    for line in text.split("\n"):
        line = re.sub(r"--.*$", "", line)
        line = re.sub(r"//.*$", "", line)
        out.append(line)
    return "\n".join(out)


def _taxo_master_line_windows(lines):
    """Return the set of 0-based line indices within a taxo.master statement
    window (the mention line plus the following ~40 lines up to the next
    statement terminator `;` or a backtick break). Used by G1."""
    idx = set()
    for i, ln in enumerate(lines):
        if re.search(r"\btaxo\.master\b", ln):
            idx.add(i)
            for j in range(i + 1, min(i + 45, len(lines))):
                idx.add(j)
                if lines[j].rstrip().endswith(";") or lines[j].strip() == "`":
                    break
            for j in range(max(0, i - 30), i):
                idx.add(j)
    return idx


def _taxo_master_blocks(lines):
    """Per-statement blocks around each taxo.master mention:
    (start, end, taxo_aliases, has_other_tables).

    `taxo_aliases` = aliases bound to taxo.master in the block (e.g. `m` from
    `taxo.master AS m`). `has_other_tables` = True when the block's FROM/JOIN
    clauses reference any table other than `taxo.master`. G2 uses these to flag
    a bad column ONLY when it is an actual taxo.master column reference — not a
    same-window `parent_id` that belongs to a different table."""
    blocks = []
    for i, ln in enumerate(lines):
        if not re.search(r"\btaxo\.master\b", ln):
            continue
        start = max(0, i - 30)
        end = i
        for j in range(i + 1, min(i + 45, len(lines))):
            end = j
            if lines[j].rstrip().endswith(";") or lines[j].strip() == "`":
                break
        text = "\n".join(lines[start:end + 1])
        aliases = set()
        for m in _TAXO_MASTER_ALIAS.finditer(text):
            a = m.group(1)
            if a and a.lower() not in _SQL_NON_ALIAS:
                aliases.add(a)
        has_other = False
        for m in _TABLE_REF.finditer(text):
            if m.group(1).lower() != "taxo.master":
                has_other = True
                break
        blocks.append((start, end, aliases, has_other))
    return blocks


def _bad_col_is_taxo_ref(prefix, aliases, has_other):
    """True when a matched bad column is an actual taxo.master column reference
    (a real G2 violation)."""
    quals = [q.strip() for q in prefix.split(".") if q.strip()]
    if not quals:
        # Bare column: belongs to taxo.master only when taxo.master is the sole
        # table in the statement (no other FROM/JOIN).
        return not has_other
    low = [q.lower() for q in quals]
    if low[-2:] == ["taxo", "master"]:
        return True                 # taxo.master.parent_id
    return quals[-1] in aliases     # <taxo_alias>.parent_id


def _scan_files(root):
    """Static scan of *.sql.ts (and versioned *.sql.vN.ts) under `root`.

    Returns (files, g1_hits, g2_hits, lower_hits) where:
      files       = sorted list of scanned paths
      g1_hits     = [(path, line, pascal_type)]      Rule 59 violations
      g2_hits     = sorted [(path, line, bad_col)]   wrong taxo.master columns
      lower_hits  = [(path, line, lowercase_type)]   candidates for the G3
                    live-DB existence check (used only by --code-db)
    """
    # Match both plain (foo.sql.ts) and versioned (foo.sql.v1.ts / foo.sql.v2.ts)
    # SQL template files. Versioned files were previously skipped by the bare
    # *.sql.ts glob (Rule 86 CI-gate fix).
    patterns = ("*.sql.ts", "*.sql.v[0-9]*.ts")
    files = sorted({
        f
        for pat in patterns
        for f in glob.glob(os.path.join(root, "**", pat), recursive=True)
    })
    g1_hits = []
    g2_set = set()
    lower_hits = []
    for path in files:
        try:
            raw = open(path, encoding="utf-8", errors="replace").read()
        except Exception as e:  # pragma: no cover
            print(f"Warning: could not read {path}: {e}")
            continue
        text = _strip_line_comments(raw)
        lines = text.split("\n")
        if not re.search(r"\btaxo\.master\b", text):
            continue  # file never touches taxo.master — out of scope
        windows = _taxo_master_line_windows(lines)

        # G1: PascalCase type literal (unchanged — critic-verified precise).
        # G3 candidates: lowercase type literal (scoped to the same windows).
        for i, ln in enumerate(lines):
            if i not in windows:
                continue
            for m in _PASCAL_TYPE.finditer(ln):
                g1_hits.append((path, i + 1, m.group(1)))
            for m in _LOWER_TYPE.finditer(ln):
                lower_hits.append((path, i + 1, m.group(1)))

        # G2: bad column reference, scoped to actual taxo.master columns.
        for start, end, aliases, has_other in _taxo_master_blocks(lines):
            for i in range(start, end + 1):
                ln = lines[i]
                for m in _BAD_COL.finditer(ln):
                    # An `AS <bad>` OUTPUT alias is not a taxo.master column
                    # reference (e.g. `SELECT value AS joining_label`).
                    if re.search(r"\bas\s*$", ln[:m.start()], re.IGNORECASE):
                        continue
                    if _bad_col_is_taxo_ref(m.group(1), aliases, has_other):
                        g2_set.add((path, i + 1, m.group(2)))
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
    print("taxo_lint --code : *.sql.ts static scan")
    print("=" * 72)
    print(f"scanned .sql.ts files              : {len(files)}")
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
    """--code-db : the --code static scan (G1 + G2) PLUS a live-DB existence
    check (G3) of every lowercase type literal found on a taxo.master query.
    G1/G2 behave exactly as in --code; G3 is the only DB-dependent addition."""
    import pymysql
    files, g1_hits, g2_hits, lower_hits = _scan_files(args.dir)

    creds = _load_db_creds(args)
    conn = pymysql.connect(connect_timeout=20, **creds)
    cur = conn.cursor()
    distinct = sorted({t for _, _, t in lower_hits})
    missing = set()
    for t in distinct:
        cur.execute(
            "SELECT COUNT(*) FROM taxo.master WHERE type = %s AND is_deleted = 0",
            (t,),
        )
        if cur.fetchone()[0] == 0:
            missing.add(t)
    conn.close()
    g3_hits = [(p, ln, t) for (p, ln, t) in lower_hits if t in missing]

    print("=" * 72)
    print("taxo_lint --code-db : *.sql.ts static scan + live-DB type existence")
    print("=" * 72)
    print(f"scanned .sql.ts files                   : {len(files)}")
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
                     help="Static scan of *.sql.ts under DIR (no DB).")
    sub.add_argument("--code-db", metavar="DIR",
                     help="Static scan of *.sql.ts under DIR PLUS a live-DB "
                          "existence check (G3) of every lowercase type literal. "
                          "Needs DB creds.")
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
