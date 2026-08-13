#!/usr/bin/env python3
"""Offline self-test for checker.py's block-scoped 2.2 resolution.

Also guards: rule 2.6 (missing is_active = 1, 2026-08-13) and its clause-scoping
(is_active on a neighbouring join must not bleed onto a different one, 2026-08-13).
Guards the 2026-07-31 fix: two adjacent taxo.master queries at different levels in
one file must be judged independently — a wrong level in query A must not mask or
mis-attribute to query B, and vice versa. Run in CI (self-test-gates.yml). Exits 1
if the checker regresses.
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "checker.py")
FIX = os.path.join(HERE, "test-fixtures")
CONTRACT = os.path.join(FIX, "contract.json")
SCHEMA = os.path.join(FIX, "schema.json")

fails = []


def run(case):
    r = subprocess.run(
        [sys.executable, CHECKER, "--contract", CONTRACT, "--schema", SCHEMA,
         "--repo-root", os.path.join(FIX, case)],
        text=True, capture_output=True)
    return r.returncode, r.stderr


def wrong_level_lines(stderr):
    return [ln for ln in stderr.splitlines() if "WRONG LEVEL" in ln]


# 1. pass fixture: two adjacent correct queries -> green, no reds
code, err = run("pass")
if code != 0 or wrong_level_lines(err):
    fails.append(f"pass fixture should be clean; exit={code}\n{err}")

# 2. wrongA: query A wrong (category->leaf), B correct -> exactly one WRONG LEVEL,
#    citing influencer_category_idfr, NOT org_department_idfr
code, err = run("fail_wrongA")
wl = wrong_level_lines(err)
if code == 0:
    fails.append("fail_wrongA not caught (exit 0)")
if len(wl) != 1 or "influencer_category_idfr" not in wl[0] or "org_department_idfr" in "".join(wl):
    fails.append(f"fail_wrongA: expected exactly one WRONG LEVEL on influencer_category_idfr; got:\n{chr(10).join(wl)}")

# 3. wrongB: query A correct, B wrong (leaf->realm) -> exactly one WRONG LEVEL,
#    citing org_department_idfr, NOT influencer_category_idfr
code, err = run("fail_wrongB")
wl = wrong_level_lines(err)
if code == 0:
    fails.append("fail_wrongB not caught (exit 0)")
if len(wl) != 1 or "org_department_idfr" not in wl[0] or "influencer_category_idfr" in "".join(wl):
    fails.append(f"fail_wrongB: expected exactly one WRONG LEVEL on org_department_idfr; got:\n{chr(10).join(wl)}")

# 4. neighbour-join-bleed: two adjacent JOINs in one block, the first asserting a
#    hierarchy_level and the second (a different column, no level predicate of
#    its own) must NOT inherit the first join's level literal by proximity.
#    Regression guard for the false-RED found reviewing PR #32's contract change
#    (2026-08-13): explore-organization.sql.ts's org_purpose_sub_category_idfr /
#    org_mission_sub_category_idfr joins carry no level literal post-PR-#1130,
#    but sit two lines below a size join's hierarchy_level='leaf' in the same
#    backtick block -- block-scoped _closest() alone picked that up as theirs.
code, err = run("pass_neighbor_join_bleed")
wl = wrong_level_lines(err)
if code != 0 or wl:
    fails.append(f"pass_neighbor_join_bleed: a join with no level literal must not "
                 f"inherit a NEIGHBOURING join's level; exit={code}\n{chr(10).join(wl)}")


# 4. fail_missing_is_active: same two queries as (the old) pass fixture minus
#    is_active = 1 -> two RED 2.6 findings (one per query), zero WRONG LEVEL.
code, err = run("fail_missing_is_active")
ia = [ln for ln in err.splitlines() if "omits is_active" in ln]
if code == 0:
    fails.append("fail_missing_is_active not caught (exit 0)")
if len(ia) != 2:
    fails.append(f"fail_missing_is_active: expected 2 'omits is_active' findings; got:\n{chr(10).join(ia)}")
if wrong_level_lines(err):
    fails.append(f"fail_missing_is_active should not trigger WRONG LEVEL:\n{chr(10).join(wrong_level_lines(err))}")

# 5. fail_is_active_clause_bleed: two JOINs in ONE block, only the first has is_active = 1.
#    The second (different column) must still RED -- its neighbour's is_active must not
#    bleed across via block-wide (rather than clause-scoped) resolution.
code, err = run("fail_is_active_clause_bleed")
ia2 = [ln for ln in err.splitlines() if "omits is_active" in ln]
if code == 0:
    fails.append("fail_is_active_clause_bleed not caught (exit 0)")
if len(ia2) != 1 or "org_mission_category_idfr" not in ia2[0] or "org_purpose_category_idfr" in "".join(ia2):
    fails.append(f"fail_is_active_clause_bleed: expected exactly one 'omits is_active' finding on org_mission_category_idfr; got:\n{chr(10).join(ia2)}")

# 6. is_active_exempt_glob: identical query under src/api-admin/** (contract exempts
#    this glob) and src/api/** (not exempt) -- admin copy must be clean, non-admin
#    copy must still RED. Regression guard for the glob-vs-exact-membership bug
#    found via live verification (2026-08-13): rule 2.6 originally did `rel not in
#    is_active_exempt`, an exact dict-key check, so a glob key like
#    'src/api-admin/**' silently exempted nothing.
code, err = run("is_active_exempt_glob")
ia3 = [ln for ln in err.splitlines() if "omits is_active" in ln]
if len(ia3) != 1 or "src/api/y/q.sql.ts" not in ia3[0] or "src/api-admin" in "".join(ia3):
    fails.append(f"is_active_exempt_glob: expected exactly one 'omits is_active' finding, "
                 f"on src/api/y/q.sql.ts only (admin copy must be exempt); got:\n{chr(10).join(ia3)}")

# 7. fail_suffix_match_wrong_level: identifier is a bare/unprefixed suffix of a
#    contract column ('department_idfr' vs 'org_department_idfr') -- rule 2.2 must
#    still resolve it (suffix-match) and catch the wrong level.
code, err = run("fail_suffix_match_wrong_level")
wl7 = wrong_level_lines(err)
if code == 0:
    fails.append("fail_suffix_match_wrong_level not caught (exit 0)")
if len(wl7) != 1 or "org_department_idfr" not in wl7[0]:
    fails.append(f"fail_suffix_match_wrong_level: expected one WRONG LEVEL on org_department_idfr; got:\n{chr(10).join(wl7)}")

# 8. fail_annotation_wrong_level: camelCase param ('deptId') has no '..._idfr' text at
#    all, so identifier-based resolution can't even attempt a match -- a voluntary
#    // TAXO_CONTRACT: type@level annotation must be used as the fallback source of
#    truth and still catch the wrong level. Regression guard for the real bug that
#    slipped through in user_mission.validator.ts (rule 2.6 caught it, 2.2 never did).
code, err = run("fail_annotation_wrong_level")
va8 = [ln for ln in err.splitlines() if "vs annotation" in ln]
if code == 0:
    fails.append("fail_annotation_wrong_level not caught (exit 0)")
if len(va8) != 1:
    fails.append(f"fail_annotation_wrong_level: expected one WRONG LEVEL vs annotation finding; got:\n{chr(10).join(va8)}")

if fails:
    print("checker self-test RED:")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("checker self-test GREEN — block-scoped 2.2 judges adjacent queries independently.")
