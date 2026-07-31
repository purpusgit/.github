#!/usr/bin/env python3
"""Offline regression test for taxo_lint --code scan scope (Fable decisions 1+2a).

Asserts the broadened glob (migrations *.sql, template-scoped inline SQL in .ts)
flags real bugs and the parser rejects the false-positive classes: TS unions, spec
files, ${} interpolation, backups/dumps. Run in CI (self-test-gates.yml)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taxo_lint

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxo-lint-fixtures")
files, g1, g2, lower = taxo_lint._scan_files(FIX)
flags = {(os.path.relpath(p, FIX).replace(os.sep, "/"), v) for p, _, v in g1}
rels = {os.path.relpath(p, FIX).replace(os.sep, "/") for p in files}

def has(rel, v): return (rel, v) in flags
checks = [
 ("migration Org_Department flagged", has("migrations/001_seed.sql", "Org_Department")),
 ("mixed.service.ts Org_Department flagged", has("src/services/mixed.service.ts", "Org_Department")),
 ("attack Line1_Bad flagged", has("src/services/attack.sql.ts", "Line1_Bad")),
 ("attack Real_Bad flagged", has("src/services/attack.sql.ts", "Real_Bad")),
 ("attack Quote_Bad flagged", has("src/services/attack.sql.ts", "Quote_Bad")),
 ("union System_Notice NOT flagged", not has("src/services/mixed.service.ts", "System_Notice")),
 ("union User_Message NOT flagged", not has("src/services/mixed.service.ts", "User_Message")),
 ("lowercase o_department NOT G1-flagged", not any(v == "o_department" for _, v in flags)),
 ("spec Fixture_Type NOT flagged", not has("src/spec/foo.spec.ts", "Fixture_Type")),
 ("nested-in-${} Nested_Ignored NOT flagged", not has("src/services/attack.sql.ts", "Nested_Ignored")),
 ("backups/ not scanned", not any("backups/" in r for r in rels)),
 ("dumps/ not scanned", not any("dumps/" in r for r in rels)),
 ("spec file not scanned", "src/spec/foo.spec.ts" not in rels),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("PASS " if ok else "FAIL ") + n)
print()
if bad:
    print(f"RED — {len(bad)} check(s) failed"); sys.exit(1)
print("GREEN — taxo_lint scan scope behaves (decisions 1 + 2a)")
