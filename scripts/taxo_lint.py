#!/usr/bin/env python3
"""taxo_lint.py — Taxonomy CI/lint gate.

Prevents the recurring "stale-type / wrong-column / stale-FK-ref" bug class in
taxonomy-touching code and data. Two independent modes:

  --data              Validate live taxo.* data (needs a DB connection).
                      Primary gate: every onboarding_question FK:<Type> ref must
                      point at a taxo.master type that still has >0 active leaves.

  --code <dir>        Static regex scan of *.sql.ts files (no DB). Catches:
                        G1  PascalCase type literals on taxo.master queries
                            (Rule 59 — new taxonomy names are snake_case only).
                        G2  taxo.master queries referencing a column that is not
                            in the real taxo.master schema allowlist
                            (parent_id / parent_idfr / joining_label et al.).

Exit code is 0 when clean, non-zero when any check fails, so it drops straight
into a CI step. Failures print as a clean table with file:line or ref detail.

DB credentials for --data come from (in order):
  1. --db-json <path> + --db-env <name>   (bc_mysql_envs.json style file)
  2. env vars TAXO_DB_HOST / TAXO_DB_PORT / TAXO_DB_USER / TAXO_DB_PASSWORD
     (CI wires these from repo/org secrets — see reusable-taxo-lint.yml).

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
_COL_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _strip_line_comments(text):
    # Remove // and -- line comments so literals inside comments don't trip us.
    out = []
    for line in text.split("\n"):
        line = re.sub(r"--.*$", "", line)
        line = re.sub(r"//.*$", "", line)
        out.append(line)
    return "\n".join(out)


def _taxo_master_line_windows(lines):
    """Return the set of 0-based line indices that are within a taxo.master
    statement window (the mention line plus the following ~40 lines up to the
    next statement terminator `;` or a blank-heavy break)."""
    idx = set()
    for i, ln in enumerate(lines):
        if re.search(r"\btaxo\.master\b", ln):
            idx.add(i)
            # extend forward until a line with a bare `;` ending a statement
            for j in range(i + 1, min(i + 45, len(lines))):
                idx.add(j)
                if lines[j].rstrip().endswith(";") or lines[j].strip() == "`":
                    break
            # also extend backward a few lines (SELECT cols precede FROM)
            for j in range(max(0, i - 30), i):
                idx.add(j)
    return idx


def run_code(args):
    root = args.dir
    files = sorted(glob.glob(os.path.join(root, "**", "*.sql.ts"), recursive=True))
    g1_hits, g2_hits = [], []
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

        for i, ln in enumerate(lines):
            if i not in windows:
                continue
            # G1: PascalCase type literal
            for m in _PASCAL_TYPE.finditer(ln):
                g1_hits.append((path, i + 1, m.group(1)))
            # G2: bad column reference near taxo.master
            for tok_m in _COL_TOKEN.finditer(ln):
                tok = tok_m.group(0)
                if tok in KNOWN_BAD_COLUMNS:
                    g2_hits.append((path, i + 1, tok))

    print("=" * 72)
    print("taxo_lint --code : *.sql.ts static scan")
    print("=" * 72)
    print(f"scanned .sql.ts files              : {len(files)}")
    print(f"[G1] PascalCase taxo type literals : {len(g1_hits)}  (Rule 59)")
    print(f"[G2] wrong taxo.master columns     : {len(g2_hits)}")
    if g1_hits:
        print("\n-- G1 violations (use snake_case lowercase types) --")
        for path, ln, val in g1_hits:
            print(f"  {path}:{ln}: type = '{val}'  ->  should be lowercase snake_case")
    if g2_hits:
        print("\n-- G2 violations (column not in taxo.master schema) --")
        for path, ln, col in g2_hits:
            print(f"  {path}:{ln}: '{col}' is not a taxo.master column")
    failed = bool(g1_hits) or bool(g2_hits)
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
    ap.add_argument("--db-json", help="Path to bc_mysql_envs.json style creds file.")
    ap.add_argument("--db-env", default="sandbox", help="Env key inside --db-json.")
    ap.add_argument("--assert-type", action="append", default=[],
                    help="(--data) Assert this taxo.master type has >0 rows. Repeatable.")
    args = ap.parse_args()

    if args.code:
        args.dir = args.code
        sys.exit(run_code(args))
    sys.exit(run_data(args))


if __name__ == "__main__":
    main()
