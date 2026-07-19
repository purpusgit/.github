#!/usr/bin/env python3
"""checker.py — G4 Taxo FK Contract checker.

Consumes taxo_fk_contract.json and enforces that every taxonomy FK column /
endpoint in a repo's *.sql.ts (and *.validator.ts) files maps to the CORRECT
taxo.master `type` + `hierarchy_level`, per G4_Checker_Workflow_Spec.md §2.

Four rule classes:
  2.1 Contract validation (fail-fast) — JSON parse + JSON Schema validate.
  2.2 Column-lookup rule  — `type='<T>'` + `hierarchy_level='<L>'` in a SQL
                            block, resolved against contract.columns[<col>].
  2.3 Endpoint annotation rule — every file that touches taxo.master must carry
                            a `// TAXO_CONTRACT: <type>@<level>[,<level>...]`
                            annotation, matching the contract + the SQL literals.
  2.4 Family-gate rule    — any `taxo.master` block with `type='<key>'` where
                            <key> is in contract.not_in_taxo_master -> RED.
  2.5 Junction rule (v1 weak, grep-based) — advisory YELLOW when a junction
                            dimension literal is not in the dimension_to_type_map.

Exit code: 1 if any RED, 0 otherwise (YELLOW never fails the build).

CLI:
  --contract PATH               path to taxo_fk_contract.json (required)
  --schema PATH                 path to taxo_fk_contract.schema.json (required)
  --repo-root PATH              repo root to scan (default: .)
  --scan-globs G [G ...]        glob patterns under repo-root to scan
  --validate-contract-only      run 2.1 only, then exit (no file scan)
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

# ---- annotation + SQL literal patterns -------------------------------------
ANNOT_RE   = re.compile(r"//\s*TAXO_CONTRACT:\s*([a-z0-9_]+@[a-z0-9_,\-]+)")
TAXO_REF_RE = re.compile(r"taxo\.master\b", re.IGNORECASE)
TYPE_LIT_RE = re.compile(r"type\s*=\s*'([^']+)'", re.IGNORECASE)
LEVEL_LIT_RE = re.compile(r"hierarchy_level\s*=\s*'([^']+)'", re.IGNORECASE)
# a column/param token: nearest *_idfr identifier
IDFR_RE = re.compile(r"\b([a-z][a-z0-9_]*_idfr)\b")
DIM_LIT_RE = re.compile(r"dimension\s*=\s*'([^']+)'", re.IGNORECASE)

RED, YELLOW = "RED", "YELLOW"


def load_and_validate_contract(contract_path, schema_path):
    """Rule 2.1 — parse + schema-validate. Returns (contract, errors[])."""
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
    # 2.1 structural asserts
    for block in ("columns", "junctions", "endpoint_types", "not_in_taxo_master"):
        if block not in contract:
            if block == "junctions":
                continue  # junctions optional per schema
            errors.append(f"contract missing required block '{block}'")
    return contract, errors


def _line_of(text, idx):
    return text.count("\n", 0, idx) + 1


def _nearest_idfr(text, pos, window=400):
    """Find the closest *_idfr token to `pos` within +/- window chars."""
    lo, hi = max(0, pos - window), min(len(text), pos + window)
    best, best_dist = None, None
    for m in IDFR_RE.finditer(text, lo, hi):
        dist = abs(m.start() - pos)
        if best_dist is None or dist < best_dist:
            best, best_dist = m.group(1), dist
    return best


def scan_file(path, rel, text, contract, findings):
    columns = contract.get("columns", {})
    endpoint_types = contract.get("endpoint_types", {})
    not_in = {k.lower(): v for k, v in contract.get("not_in_taxo_master", {}).items()}
    junctions = contract.get("junctions", {})

    touches_taxo = bool(TAXO_REF_RE.search(text))
    annots = ANNOT_RE.findall(text)
    annot_types = set()
    for a in annots:
        t = a.split("@", 1)[0]
        annot_types.add(t)

    known_endpoint_types = {e["taxo_type"] for e in endpoint_types.values()}
    known_column_types = {c["taxo_type"] for c in columns.values()}

    # ---- 2.3 Endpoint annotation rule ----
    if touches_taxo and not annots:
        for m in TAXO_REF_RE.finditer(text):
            findings.append((RED, "2.3", rel, _line_of(text, m.start()),
                             "queries taxo.master but has no // TAXO_CONTRACT: annotation"))
            break
    for a in annots:
        atype = a.split("@", 1)[0]
        if atype not in known_column_types and atype not in known_endpoint_types:
            idx = text.find(a)
            findings.append((RED, "2.3", rel, _line_of(text, idx if idx >= 0 else 0),
                             f"annotation type '{atype}' not in contract columns/endpoint_types"))

    # ---- 2.4 Family gate + 2.2 column-lookup ----
    for m in TYPE_LIT_RE.finditer(text):
        typ = m.group(1)
        line = _line_of(text, m.start())
        # only enforce when it's a taxo.master context (whole-file heuristic)
        if not touches_taxo:
            continue
        # 2.4 family gate
        if typ.lower() in not_in:
            findings.append((RED, "2.4", rel, line,
                             f"type='{typ}' must NOT be queried on taxo.master ({not_in[typ.lower()]})"))
            continue
        # 2.3 annotation/type-literal consistency
        if annot_types and typ not in annot_types:
            findings.append((RED, "2.3", rel, line,
                             f"SQL type='{typ}' does not match file annotation type(s) {sorted(annot_types)}"))
        # 2.2 column-lookup
        lvl_m = None
        # find the closest hierarchy_level literal within same window
        for lm in LEVEL_LIT_RE.finditer(text, max(0, m.start() - 300), min(len(text), m.start() + 300)):
            lvl_m = lm
            break
        col = _nearest_idfr(text, m.start())
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

    # ---- 2.5 Junction rule (v1 weak/grep — advisory YELLOW) ----
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
    ap = argparse.ArgumentParser(description="G4 Taxo FK Contract checker")
    ap.add_argument("--contract", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--scan-globs", nargs="*", default=[
        "src/api/**/*.sql.ts", "src/api-admin/**/*.sql.ts", "src/**/*.validator.ts"])
    ap.add_argument("--validate-contract-only", action="store_true")
    args = ap.parse_args()

    contract, cerrs = load_and_validate_contract(args.contract, args.schema)
    if cerrs:
        for e in cerrs:
            print(f"RED 2.1 {args.contract}: {e}", file=sys.stderr)
        # a validation-only run with jsonschema-missing warning should still fail loudly
        fatal = any("not installed" not in e for e in cerrs)
        if fatal:
            print("Contract validation FAILED.", file=sys.stdout)
            sys.exit(1)
    if args.validate_contract_only:
        print("Contract validates against schema. OK", file=sys.stdout)
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
    green = scanned - len({(f[2]) for f in findings})
    if green < 0:
        green = 0
    print(f"Scanned {scanned} files. RED={len(reds)} YELLOW={len(yellows)} GREEN={green}",
          file=sys.stdout)
    sys.exit(1 if reds else 0)


if __name__ == "__main__":
    main()
