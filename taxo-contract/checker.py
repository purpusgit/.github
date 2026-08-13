#!/usr/bin/env python3
"""checker.py — G4 Taxo FK Contract checker.

2026-07-31 revision (per Fable review of the rules-to-gates audit):
  * Rule 2.3's MANDATE ARM is removed. It previously RED-walled every file that
    queried taxo.master without a `// TAXO_CONTRACT:` annotation. Zero files in the
    org carry that annotation, so it enforced nothing but an org-wide red wall, and
    rule 2.2 (the actual wrong-level check) reads the CONTRACT, never the annotation
    — so the mandate bought zero level protection. The residual type-consistency
    check is kept only as a YELLOW advisory, and only when an annotation is
    voluntarily present.
  * Rule 2.2 is block-scoped. It used to take the FIRST hierarchy_level literal in a
    ±300 window (positionally earliest, often from the PREVIOUS query) and resolve
    the column from a SEPARATE ±400 window — the two windows had no shared boundary,
    so it could pair a column from query A with a level from query B and RED a
    correct file (or stay silent on a wrong one). Now both the column and the level
    are resolved within the SAME SQL block (the enclosing backtick template literal)
    and by CLOSEST match to the `type=` literal, not first-in-window.
  * --advisory makes the gate report reds but exit 0, so it can be wired non-blocking
    on a repo before its pre-existing state is known (mirrors reusable-taxo-lint.yml).

2026-08-13 fix (external critic follow-up on PR #32): _closest() resolved the
  level within the whole SQL block, so a NEIGHBOURING join's hierarchy_level
  literal could be misattributed to a different column with no level literal of
  its own. Added _clause_bounds() to narrow resolution to the single JOIN...ON
  clause containing the type literal.
"""
import argparse
import glob
import json
import os
import re
import sys

try:
    import jsonschema
except ImportError:
    jsonschema = None

ANNOT_RE     = re.compile(r"//\s*TAXO_CONTRACT:\s*([a-z0-9_]+@[a-z0-9_,\-]+)")
TAXO_REF_RE  = re.compile(r"taxo\.master\b", re.IGNORECASE)
TYPE_LIT_RE  = re.compile(r"type\s*=\s*'([^']+)'", re.IGNORECASE)
LEVEL_LIT_RE = re.compile(r"hierarchy_level\s*=\s*'([^']+)'", re.IGNORECASE)
IDFR_RE      = re.compile(r"\b([a-z][a-z0-9_]*_idfr)\b")
JOIN_RE      = re.compile(r"\bJOIN\b", re.IGNORECASE)
DIM_LIT_RE   = re.compile(r"dimension\s*=\s*'([^']+)'", re.IGNORECASE)

RED, YELLOW = "RED", "YELLOW"


def load_and_validate_contract(contract_path, schema_path):
    errors = []
    try:
        with open(contract_path, encoding="utf-8") as f:
            contract = json.load(f)
    except Exception as e:
        return None, [f"contract JSON parse failed: {e}"]
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except Exception as e:
        return None, [f"schema JSON parse failed: {e}"]
    if jsonschema is None:
        errors.append("jsonschema module not installed — cannot validate contract")
        return contract, errors
    try:
        jsonschema.validate(instance=contract, schema=schema)
    except jsonschema.ValidationError as e:
        errors.append(f"contract fails schema at {list(e.absolute_path)}: {e.message}")
        return contract, errors
    for block in ("columns", "endpoint_types", "not_in_taxo_master"):
        if block not in contract:
            errors.append(f"contract missing required block '{block}'")
    return contract, errors


def _line_of(text, idx):
    return text.count("\n", 0, idx) + 1


def _block_bounds(text, pos, fallback=250):
    """The SQL block containing `pos` — the enclosing backtick template literal.
    Falls back to a symmetric window when the type literal isn't inside backticks.
    This is the shared boundary the old two-window design lacked."""
    lo = text.rfind("`", 0, pos)
    hi = text.find("`", pos)
    if lo != -1 and hi != -1 and lo < pos < hi:
        return lo + 1, hi
    return max(0, pos - fallback), min(len(text), pos + fallback)


def _closest(regex, text, lo, hi, pos):
    """The regex match nearest to `pos` within [lo, hi) — not the first."""
    best, best_d = None, None
    for m in regex.finditer(text, lo, hi):
        d = abs(m.start() - pos)
        if best_d is None or d < best_d:
            best, best_d = m, d
    return best


def _clause_bounds(text, lo, hi, pos):
    """Narrow [lo, hi) (the whole SQL block) to the single JOIN ... ON clause
    containing `pos`. Without this, _closest() can pick up a hierarchy_level
    literal that belongs to a NEIGHBOURING join on an adjacent line of the same
    backtick block — e.g. a size/type join's `hierarchy_level = 'leaf'` read as
    the level for a category join two lines below that carries no level
    predicate at all. Falls back to the full block when no JOIN keyword is
    found (a plain SELECT ... WHERE with no join to scope to)."""
    starts = [m.start() for m in JOIN_RE.finditer(text, lo, hi) if m.start() <= pos]
    ends = [m.start() for m in JOIN_RE.finditer(text, lo, hi) if m.start() > pos]
    return (starts[-1] if starts else lo), (ends[0] if ends else hi)


def scan_file(path, rel, text, contract, findings):
    columns = contract.get("columns", {})
    endpoint_types = contract.get("endpoint_types", {})
    not_in = {k.lower(): v for k, v in contract.get("not_in_taxo_master", {}).items()}
    junctions = contract.get("junctions", {})

    touches_taxo = bool(TAXO_REF_RE.search(text))
    annots = ANNOT_RE.findall(text)
    annot_types = {a.split("@", 1)[0] for a in annots}

    known_endpoint_types = {e["taxo_type"] for e in endpoint_types.values()}
    known_column_types = {c["taxo_type"] for c in columns.values()}

    # 2.3 — ADVISORY ONLY, and only when an annotation is voluntarily present.
    # The mandate arm ("touches taxo.master but not annotated -> RED") is gone.
    for a in annots:
        atype = a.split("@", 1)[0]
        if atype not in known_column_types and atype not in known_endpoint_types:
            idx = text.find(a)
            findings.append((YELLOW, "2.3", rel, _line_of(text, idx if idx >= 0 else 0),
                             f"annotation type '{atype}' not in contract columns/endpoint_types"))

    for m in TYPE_LIT_RE.finditer(text):
        typ = m.group(1)
        line = _line_of(text, m.start())
        if not touches_taxo:
            continue

        # 2.4 — RED: this type must never be queried on taxo.master.
        if typ.lower() in not_in:
            findings.append((RED, "2.4", rel, line,
                             f"type='{typ}' must NOT be queried on taxo.master ({not_in[typ.lower()]})"))
            continue

        # 2.3 residual — YELLOW advisory: SQL type disagrees with the file's
        # voluntarily-declared annotation type(s). Only fires when annotated.
        if annot_types and typ not in annot_types:
            findings.append((YELLOW, "2.3", rel, line,
                             f"SQL type='{typ}' does not match file annotation type(s) {sorted(annot_types)}"))

        # 2.2 — RED: wrong concept / wrong level. Column AND level resolved within
        # the SAME SQL block, by closest match to this type literal.
        lo, hi = _block_bounds(text, m.start())
        clo, chi = _clause_bounds(text, lo, hi, m.start())
        lvl_m = _closest(LEVEL_LIT_RE, text, clo, chi, m.start())
        col_m = _closest(IDFR_RE, text, clo, chi, m.start())
        col = col_m.group(1) if col_m else None
        if col and col in columns:
            spec = columns[col]
            if spec["taxo_type"] != typ:
                findings.append((RED, "2.2", rel, line,
                                 f"column '{col}' -> contract type '{spec['taxo_type']}' but SQL uses type='{typ}' (WRONG CONCEPT)"))
            want_lvl = spec["hierarchy_level"]
            if want_lvl not in ("any", "multi") and lvl_m and lvl_m.group(1) != want_lvl:
                findings.append((RED, "2.2", rel, line,
                                 f"column '{col}' -> contract level '{want_lvl}' but SQL uses hierarchy_level='{lvl_m.group(1)}' (WRONG LEVEL)"))
            if want_lvl == "multi" and lvl_m and lvl_m.group(1) not in spec.get("levels", []):
                findings.append((RED, "2.2", rel, line,
                                 f"column '{col}' multi-level {spec.get('levels')} but SQL uses '{lvl_m.group(1)}'"))

    for tbl, jspec in junctions.items():
        short = tbl.split(".")[-1]
        if short in text:
            dmap = jspec.get("dimension_to_type_map", {})
            for dm in DIM_LIT_RE.finditer(text):
                dim = dm.group(1)
                if dim not in dmap:
                    findings.append((YELLOW, "2.5", rel, _line_of(text, dm.start()),
                                     f"junction '{tbl}' dimension='{dim}' not in dimension_to_type_map (v1 grep-advisory)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--scan-globs", nargs="*", default=[
        "src/api/**/*.sql.ts", "src/api-admin/**/*.sql.ts", "src/**/*.validator.ts"])
    ap.add_argument("--validate-contract-only", action="store_true")
    ap.add_argument("--advisory", action="store_true",
                    help="report reds but exit 0 (non-blocking rollout)")
    args = ap.parse_args()

    contract, cerrs = load_and_validate_contract(args.contract, args.schema)
    if cerrs:
        for e in cerrs:
            print(f"RED 2.1 {args.contract}: {e}", file=sys.stderr)
        if any("not installed" not in e for e in cerrs):
            print("Contract validation FAILED.")
            sys.exit(1)
    if args.validate_contract_only:
        print("Contract validates against schema. OK")
        sys.exit(0)

    findings = []
    scanned = 0
    for pattern in args.scan_globs:
        for path in glob.glob(os.path.join(args.repo_root, pattern), recursive=True):
            if not os.path.isfile(path):
                continue
            scanned += 1
            rel = os.path.relpath(path, args.repo_root)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception as e:
                findings.append((RED, "io", rel, 0, f"cannot read: {e}"))
                continue
            scan_file(path, rel, text, contract, findings)

    reds = [f for f in findings if f[0] == RED]
    yellows = [f for f in findings if f[0] == YELLOW]
    for sev, rule, rel, line, detail in findings:
        print(f"{sev} {rule} {rel}:{line} {detail}", file=sys.stderr)
    print(f"Scanned {scanned} files. RED={len(reds)} YELLOW={len(yellows)}")
    if args.advisory:
        if reds:
            print("advisory mode: reds present but exiting 0 (non-blocking).")
        sys.exit(0)
    sys.exit(1 if reds else 0)


if __name__ == "__main__":
    main()
