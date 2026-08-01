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
 # Fable FIX-FIRST: stray backtick in comments/strings must not desync template parity
 ("comment_odd: Miss_Odd flagged (stray ` in // comment)", has("src/services/comment_odd.ts", "Miss_Odd")),
 ("comment_two: First_Bad flagged (parity must not cascade)", has("src/services/comment_two.ts", "First_Bad")),
 ("comment_two: Second_Bad flagged (parity must not cascade)", has("src/services/comment_two.ts", "Second_Bad")),
 ("string_backtick: Miss_String flagged (stray ` in \" string)", has("src/services/string_backtick.ts", "Miss_String")),
 ("even_parity: Even_Ok still flagged (regression guard)", has("src/services/even_parity.ts", "Even_Ok")),
 # masker attack surface (Fable's re-attack, pre-empted)
 ("url_string: Url_Ok flagged (// inside URL string is not a comment)", has("src/services/url_string.ts", "Url_Ok")),
 ("block_in_string: BlockStr_Ok flagged (/* inside a string is not a comment)", has("src/services/block_in_string.ts", "BlockStr_Ok")),
 ("block_comment: Block_Bad flagged (stray ` in /* */ block comment)", has("src/services/block_comment.ts", "Block_Bad")),
 # M9 KNOWN-GAP (documented in _mask_ts_noncode): a JS regex literal containing a
 # backtick desyncs the masker, so this violation is MISSED. Pinned so that if
 # anyone later adds regex tracking (or the gap silently moves either direction),
 # this assertion trips and forces an intentional update.
 ("regex_gap KNOWN-GAP: Regex_Gap NOT flagged (documented regex limitation)", not has("src/services/regex_gap.ts", "Regex_Gap")),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("PASS " if ok else "FAIL ") + n)
print()
if bad:
    print(f"RED — {len(bad)} check(s) failed"); sys.exit(1)
print("GREEN — taxo_lint scan scope behaves (decisions 1 + 2a)")
