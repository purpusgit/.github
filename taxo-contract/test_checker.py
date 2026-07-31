#!/usr/bin/env python3
"""Offline self-test for checker.py's block-scoped 2.2 resolution.

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

if fails:
    print("checker self-test RED:")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("checker self-test GREEN — block-scoped 2.2 judges adjacent queries independently.")
