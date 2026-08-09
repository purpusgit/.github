#!/usr/bin/env python3
"""
Self-CI for purpusgit/.github — exercises every reusable gate's `run:` predicate.

Task 5 of the Rules-to-Gates canonicalisation spec: "a gate that never reds is
untested". The eight org gates in .github/workflows/ are called `@main` by every
consumer repo, yet .github had no CI on itself (PR #15 returned zero check runs).
This harness gives it one.

What it does, per workflow:
  1. Extracts every `run:` script via YAML (never a hand-copied duplicate — it
     reads the LIVE predicate, so it reds the moment the real gate changes shape).
  2. Substitutes `${{ ... }}` GitHub expressions the way Actions does before the
     shell sees them, then `bash -n`-checks the script. Embedded `python3 <<'PYEOF'`
     heredocs are additionally `py_compile`d — bash -n treats a heredoc body as
     inert data, so a Python syntax error in a predicate would otherwise slip past.
  3. For the SELF-CONTAINED gates (no DB, no cross-repo token, single repo),
     runs the real extracted predicate against a PASS fixture (assert exit 0) and a
     FAIL fixture (assert exit != 0).
  4. Runs a CANARY: deliberately weakens the dart predicate and asserts the FAIL
     fixture stops catching it — proving the behavioural assertions genuinely
     exercise the predicate (a test that cannot fail proves nothing).

The DELEGATING gates (taxo-lint, taxo-contract-lint, taxo-data-nightly) call
external Python that needs live MySQL secrets or a cross-repo checkout token, so
their exit codes cannot be asserted in isolation here — they get syntax-checking
only, logged SKIP-BEHAVIOURAL / DEAD-NO-CALLERS with the reason. We do not fake
coverage.

flutter-analyze and tsc-check are PARTIAL: each has exactly one self-contained
predicate (`flutter analyze lib/`, `tsc --noEmit`) and that one is behaviourally
tested — the self-test job installs the Flutter SDK and Node for precisely that.
Their other steps (private-dep resolution via GH_PAT, `npm ci` against a consumer
lockfile) still cannot run here and stay syntax-only, logged PARTIAL with the
reason rather than silently dropped.

Two rules make those two fixtures honest, and they apply to any future gate whose
behavioural step is not the last one:
  * The step is pinned BY NAME (`kind: "step"`), never by `runs[-1]`. Positional
    selection silently retargets the assertion at a different predicate the moment
    a step is added or removed; by name, the harness reds with "step no longer
    exists" and a human repoints it.
  * FAIL fixtures assert an expected diagnostic substring (`expect`), not merely a
    non-zero exit. A missing or broken toolchain exits 127, which a bare
    exit-code assertion would happily score as "violation caught" — a green
    harness proving nothing.

Exit 0 = all checks passed (green). Exit 1 = a predicate broke or a fixture
assertion failed (red).
"""
import os, re, sys, shutil, subprocess, tempfile, py_compile, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")
FIXTURES = os.path.join(ROOT, "scripts", "gate-fixtures")

# ── behavioural map: which gate is self-contained and how to run it ───────────
# kind:
#   'dir'    -> copy fixtures/<key>/{pass,fail} to a temp dir, run predicate there
#   'step'   -> same, but pick the predicate by STEP NAME instead of runs[-1];
#               optional 'prep' runs first (e.g. dependency resolution), optional
#               'expect' asserts a substring in the FAIL fixture's output, and
#               optional 'note' records which steps remain syntax-only
#   'colors' -> build a two-branch git repo at runtime (gate is diff-based)
#   'diff'   -> build a two-branch git repo; add pass/fail line (diff-based gate)
#   'barrel' -> build two-branch git repo; test removal detection
#   'rule84' -> 3-step dir fixture test (url, fallback, chat)
#   'dead'   -> 0 callers org-wide: no behavioural test, needs an existence decision
#   None     -> external: syntax-check only, with `reason`
BEHAVIOUR = {
    "reusable-dart-safety-gate.yml":            {"kind": "dir", "key": "dart"},
    "reusable-sql-safety-gate.yml":             {"kind": "dir", "key": "sql-semicolon"},
    "reusable-sql-typestring-safety-gate.yml":  {"kind": "diff", "fpath": "src/x.sql.ts",
        "base": "export const q = `SELECT id FROM taxo.master WHERE is_deleted = 0`;\n",
        "pass": "  AND type = 'org_department'", "fail": "  AND type = 'Org_Department'"},
    "reusable-colors-safety-gate.yml":          {"kind": "colors"},
    "reusable-barrel-safety-gate.yml":          {"kind": "barrel"},
    "reusable-flutter-analyze.yml":  {"kind": "step", "step": "Analyze (lib only)",
        "key": "flutter-analyze", "prep": "dart pub get --no-example",
        "expect": "unused_element",
        "note": "checkout/git-config + `dart pub get` against private org deps need the"
                " GH_PAT secret; the Test step needs a consumer test suite. Those stay"
                " syntax-only. The analyze predicate itself is behaviourally tested."},
    "reusable-tsc-check.yml":       {"kind": "step", "step": "TypeScript type-check",
        "key": "tsc", "expect": "error TS2322",
        "note": "`npm ci` needs the consumer repo's lockfile and the detect step writes to"
                " $GITHUB_OUTPUT; both stay syntax-only. The tsc predicate itself is"
                " behaviourally tested."},
    "reusable-rule84-flavor-fork-gate.yml":     {"kind": "rule84"},
    # `step`, not `dir`: the dir branch does not forward `expect`, and without one a
    # Python traceback exiting 1 scores as "violation caught". The predicate is pure
    # Python over a directory — no secrets, no network, no DB — so it earns a real
    # behavioural fixture rather than a SKIP. `expect` is verbatim gate output.
    "reusable-drizzle-journal-gate.yml": {"kind": "step",
        "step": "Check every .sql file has a matching journal entry",
        "key": "drizzle-journal",
        "expect": "ERROR: Drizzle journal completeness gate FAILED"},
    "reusable-taxo-lint.yml":        {"kind": None, "reason": "delegates to scripts/taxo_lint.py; --data needs live MySQL secrets"},
    "reusable-taxo-contract-lint.yml": {"kind": None, "reason": "wired to service_orbit_orgs (2026-08-01); needs ORG_GITHUB_READ_TOKEN cross-repo secret. checker.py offline-tested by taxo-contract/test_checker.py"},
    "taxo-data-lint-nightly.yml":    {"kind": None, "reason": "DB-backed scheduled job; needs TAXO_DB_* MySQL secrets"},
}

FAILURES = []
def fail(msg): FAILURES.append(msg); print(f"  ✗ {msg}")
def ok(msg):   print(f"  ✓ {msg}")

def load_yaml(path):
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)

def extract_runs(doc):
    """Every step.run script in the workflow, in order."""
    runs = []
    for job in (doc.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            if isinstance(step, dict) and "run" in step and step["run"]:
                runs.append(step["run"])
    return runs

def extract_named_runs(doc):
    """{step name: run script} for every named `run:` step.

    Lets a behavioural fixture pin itself to a step by NAME. Positional
    selection (runs[-1]) silently retargets when a step is added or removed;
    by name, the harness reds instead of quietly asserting on a different
    predicate than the one the fixture was written for.
    """
    named = {}
    for job in (doc.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            if isinstance(step, dict) and step.get("run") and step.get("name"):
                named[step["name"]] = step["run"]
    return named

def sub_gha(script):
    """Actions expands ${{ ... }} before the shell runs. Mirror that so the
    residual is valid bash. 'cwb' happens to also be the colours gate's default
    base_ref, so the diff fixture works with the same substitution."""
    return re.sub(r"\$\{\{.*?\}\}", "cwb", script)

def bash_n(script, label):
    r = subprocess.run(["bash", "-n"], input=sub_gha(script),
                       text=True, capture_output=True)
    if r.returncode != 0:
        fail(f"{label}: bash -n rejected predicate\n{r.stderr.strip()}")
        return False
    return True

def pycompile_heredocs(script, label):
    """Compile any python `<<'DELIM' ... DELIM` heredoc body — bash -n won't."""
    for m in re.finditer(r"<<'(\w+)'\n(.*?)\n\s*\1", script, re.DOTALL):
        body = m.group(2)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as t:
            t.write(body); tmp = t.name
        try:
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            fail(f"{label}: embedded python heredoc has a syntax error\n{e}")
        finally:
            os.unlink(tmp)

def run_predicate(script, workdir):
    """Run the (gha-substituted) predicate in workdir, return exit code."""
    r = subprocess.run(["bash", "-c", sub_gha(script)], cwd=workdir,
                       text=True, capture_output=True)
    return r.returncode, r.stdout + r.stderr

def behavioural_dir(script, key, label, prep=None, expect=None):
    src = os.path.join(FIXTURES, key)
    for case, want_zero in (("pass", True), ("fail", False)):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "repo")
            shutil.copytree(os.path.join(src, case), work)
            if prep:
                p = subprocess.run(["bash", "-c", prep], cwd=work,
                                   text=True, capture_output=True)
                if p.returncode != 0:
                    fail(f"{label}: prep {prep!r} failed on the {case} fixture "
                         f"(exit {p.returncode}) — cannot assert on the predicate\n"
                         f"{p.stdout}{p.stderr}")
                    continue
            code, out = run_predicate(script, work)
            if want_zero and code != 0:
                fail(f"{label}: PASS fixture unexpectedly RED (exit {code})\n{out}")
            elif not want_zero and code == 0:
                fail(f"{label}: FAIL fixture was NOT caught (exit 0) — gate is asleep\n{out}")
            elif not want_zero and expect and expect not in out:
                # Non-zero is necessary but not sufficient: a missing toolchain
                # exits 127 and would otherwise be scored as a caught violation.
                fail(f"{label}: FAIL fixture exited {code} but the output does not "
                     f"contain {expect!r} — the gate went red for the WRONG reason\n{out}")
            else:
                ok(f"{label}: {case} fixture behaves ("
                   f"{'exit 0' if want_zero else 'non-zero'}"
                   f"{'' if want_zero or not expect else f', reports {expect!r}'})")

def build_diff_repo(tmp, fpath, base_content, added_line, origin_ref=True):
    """Two-branch git repo the diff-based gates need. Base branch 'cwb' holds
    base_content at fpath; the work branch appends added_line.

    origin_ref=False never creates refs/remotes/origin/cwb, so the gate cannot
    resolve its base ref — the silent-pass case a diff-based gate must go RED on
    instead of reporting an empty diff as "nothing changed, passed"."""
    g = ["git", "-c", "user.email=t@t.co", "-c", "user.name=t"]
    def run(*a): subprocess.run(list(a), cwd=tmp, check=True, capture_output=True)
    run(*g, "init", "-q", "-b", "cwb")
    full = os.path.join(tmp, fpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w").write(base_content)
    run(*g, "add", "-A"); run(*g, "commit", "-qm", "base")
    if origin_ref:
        run(*g, "update-ref", "refs/remotes/origin/cwb", "HEAD")
    run(*g, "checkout", "-qb", "work")
    with open(full, "a") as f:
        f.write("\n" + added_line + "\n")
    run(*g, "add", "-A"); run(*g, "commit", "-qm", "work")

def build_barrel_repo(tmp, fpath, base_content, work_content, origin_ref=True):
    """Two-branch git repo where work branch overwrites fpath with work_content.
    Supports testing both removal (fail) and addition (pass) cases.

    base_content=None -> fpath does not exist on the base branch at all, i.e. a
    barrel file NEW in this PR. That is legitimate and must still pass; it is the
    case a naive "cat-file -e non-zero => fail" fix would have broken.
    origin_ref=False  -> refs/remotes/origin/cwb is never created, so the gate
    cannot resolve its base ref and must go RED."""
    g = ["git", "-c", "user.email=t@t.co", "-c", "user.name=t"]
    def run(*a): subprocess.run(list(a), cwd=tmp, check=True, capture_output=True)
    run(*g, "init", "-q", "-b", "cwb")
    full = os.path.join(tmp, fpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if base_content is None:
        open(os.path.join(os.path.dirname(full), "keep.txt"), "w").write("placeholder\n")
    else:
        open(full, "w").write(base_content)
    run(*g, "add", "-A"); run(*g, "commit", "-qm", "base")
    if origin_ref:
        run(*g, "update-ref", "refs/remotes/origin/cwb", "HEAD")
    run(*g, "checkout", "-qb", "work")
    open(full, "w").write(work_content)
    run(*g, "add", "-A"); run(*g, "commit", "-qm", "work")

def _assert_diff(script, label, case, want_zero, tmp, expect=None):
    code, out = run_predicate(script, tmp)
    if want_zero and code != 0:
        fail(f"{label}: {case} fixture unexpectedly RED (exit {code})\n{out}")
    elif not want_zero and code == 0:
        fail(f"{label}: {case} fixture was NOT caught (exit 0) — gate is asleep\n{out}")
    elif not want_zero and expect and expect not in out:
        # Non-zero is necessary but not sufficient: a broken predicate can exit
        # 1/127 for reasons unrelated to the violation and would otherwise be
        # scored as a catch.
        fail(f"{label}: {case} fixture exited {code} but the output does not contain "
             f"{expect!r} — the gate went red for the WRONG reason\n{out}")
    else:
        ok(f"{label}: {case} fixture behaves ({'exit 0' if want_zero else 'non-zero'}"
           f"{'' if want_zero or not expect else f', reports {expect!r}'})")

def behavioural_colors(script, label):
    for case, line, want_zero in (
        ("pass", "  final c = colorScheme.primary;", True),
        ("fail", "  final c = Colors.red;", False),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            build_diff_repo(tmp, "lib/w.dart", "class W {}\n", line)
            _assert_diff(script, label, case, want_zero, tmp,
                         expect=None if want_zero else "Hardcoded Colors.* added")
    # R2 — base ref unresolvable. The diff then comes back empty and the gate used
    # to print "No lib/ Dart files changed. Gate passed." and exit 0, i.e. a gate
    # that could not compute its diff scored itself green. Must be RED.
    with tempfile.TemporaryDirectory() as tmp:
        build_diff_repo(tmp, "lib/w.dart", "class W {}\n",
                        "  final c = Colors.red;", origin_ref=False)
        _assert_diff(script, label, "fail[base-ref-unresolvable]", False, tmp,
                     expect="could not resolve base ref")

def behavioural_diff(script, spec, label):
    for case, want_zero in (("pass", True), ("fail", False)):
        with tempfile.TemporaryDirectory() as tmp:
            build_diff_repo(tmp, spec["fpath"], spec["base"], spec[case])
            _assert_diff(script, label, case, want_zero, tmp)

def behavioural_barrel(runs, label):
    """Barrel removal detection, plus the two cases R1 separates.

    The gate's `git show`/`cat-file` probe on the base ref is non-zero for BOTH
    "file is new in this PR" (legitimate) and "base ref will not resolve"
    (uncomputable diff). The first must pass, the second must red; a fix that
    collapses them either way is wrong, so both are pinned here."""
    script = runs[-1]
    fpath = "lib/index.dart"
    base = "export 'a.dart';\nexport 'b.dart';\n"
    cases = [
        # case, base_content, work_content, want_zero, expect, origin_ref
        ("pass", base, "export 'a.dart';\nexport 'b.dart';\nexport 'c.dart';\n",
         True, None, True),
        ("fail", base, "export 'a.dart';\n", False, "RULE 66 VIOLATION", True),
        # R1 — barrel file absent at base: NEW in this PR, still a pass.
        ("pass[new-barrel-file-at-base]", None, base, True, None, True),
        # R1 — base ref unresolvable: no diff is computable, so RED.
        ("fail[base-ref-unresolvable]", base, "export 'a.dart';\n", False,
         "could not resolve base ref", False),
    ]
    for case, base_content, work_content, want_zero, expect, origin_ref in cases:
        with tempfile.TemporaryDirectory() as tmp:
            build_barrel_repo(tmp, fpath, base_content, work_content,
                              origin_ref=origin_ref)
            _assert_diff(script, label, case, want_zero, tmp, expect=expect)

def behavioural_rule84(runs, label):
    """Test all 3 rule84 steps against their respective fixtures."""
    if len(runs) < 3:
        fail(f"{label}: expected 3 run: steps, got {len(runs)}")
        return
    behavioural_dir(runs[0], "rule84-url",      label + "[url]")
    behavioural_dir(runs[1], "rule84-fallback",  label + "[fallback]")
    behavioural_dir(runs[2], "rule84-chat",      label + "[chat]")

def canary(dart_script):
    """Negative test of the test: weaken the dart predicate (strip its `exit 1`)
    and confirm the FAIL fixture stops being caught."""
    weak = dart_script.replace("exit 1", "exit 0")
    if weak == dart_script:
        fail("canary: could not locate `exit 1` to weaken — canary is inert")
        return
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "repo")
        shutil.copytree(os.path.join(FIXTURES, "dart", "fail"), work)
        code, _ = run_predicate(weak, work)
    if code == 0:
        ok("canary: weakened dart predicate goes green on the FAIL fixture — "
           "behavioural assertions genuinely exercise the predicate")
    else:
        fail(f"canary: weakened predicate still exited {code}; the FAIL fixture "
             "does not actually depend on the predicate")

def canary_analyze(analyze_script):
    """Second canary, for the analyze gate specifically.

    The flags are the whole predicate here: `--no-fatal-infos` is deliberate,
    `--no-fatal-warnings` would not be. Weaken the invocation exactly that way
    and assert the FAIL fixture (a warning-level `unused_element`) stops being
    caught — which is what proves the fixture tests the WARNING bar and not just
    'analyze ran'.
    """
    weak = analyze_script.replace("--no-fatal-infos", "--no-fatal-infos --no-fatal-warnings")
    if weak == analyze_script:
        fail("canary[analyze]: `--no-fatal-infos` not found in the analyze predicate — "
             "the gate changed shape; repoint this canary")
        return
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "repo")
        shutil.copytree(os.path.join(FIXTURES, "flutter-analyze", "fail"), work)
        p = subprocess.run(["bash", "-c", "dart pub get --no-example"], cwd=work,
                           text=True, capture_output=True)
        if p.returncode != 0:
            fail(f"canary[analyze]: prep failed\n{p.stdout}{p.stderr}")
            return
        code, _ = run_predicate(weak, work)
    if code == 0:
        ok("canary[analyze]: predicate weakened to --no-fatal-warnings goes green on "
           "the FAIL fixture — the fixture genuinely tests the warning bar")
    else:
        fail(f"canary[analyze]: weakened predicate still exited {code}; the FAIL "
             "fixture is red for some reason other than the planted warning")

def main():
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(WF_DIR, "*.yml")))
    files = [f for f in files if f != "self-test-gates.yml"]
    print(f"Found {len(files)} workflow(s) in .github/workflows/\n")

    dart_script = None
    analyze_script = None
    for fn in files:
        print(f"── {fn}")
        doc = load_yaml(os.path.join(WF_DIR, fn))
        runs = extract_runs(doc)
        named = extract_named_runs(doc)
        if not runs:
            print("  (no run: steps)")
        for i, script in enumerate(runs):
            label = f"{fn}[run#{i+1}]"
            if bash_n(script, label):
                ok(f"{label}: bash -n clean")
            pycompile_heredocs(script, label)

        beh = BEHAVIOUR.get(fn)
        if beh is None:
            print(f"  ! no behaviour entry for {fn} — add one (new gate?)")
            fail(f"{fn}: unmapped gate; refusing to silently skip")
        elif beh["kind"] == "dead":
            print(f"  ⚰ DEAD-NO-CALLERS: {beh['reason']}")
        elif beh["kind"] is None:
            print(f"  ⤳ SKIP-BEHAVIOURAL: {beh['reason']}")
        elif beh["kind"] == "dir":
            behavioural_dir(runs[-1], beh["key"], fn)
            if beh.get("key") == "dart":
                dart_script = runs[-1]
        elif beh["kind"] == "step":
            script = named.get(beh["step"])
            if script is None:
                fail(f"{fn}: behavioural step {beh['step']!r} no longer exists in this "
                     "gate — the gate changed shape. Repoint or remove the fixture; "
                     "do NOT let it fall back to a positional guess.")
            else:
                behavioural_dir(script, beh["key"], fn,
                                prep=beh.get("prep"), expect=beh.get("expect"))
                if beh["key"] == "flutter-analyze":
                    analyze_script = script
            if beh.get("note"):
                print(f"  ⤳ PARTIAL: {beh['note']}")
        elif beh["kind"] == "colors":
            behavioural_colors(runs[-1], fn)
        elif beh["kind"] == "diff":
            behavioural_diff(runs[-1], beh, fn)
        elif beh["kind"] == "barrel":
            behavioural_barrel(runs, fn)
        elif beh["kind"] == "rule84":
            behavioural_rule84(runs, fn)
        print()

    print("── canary (negative test of the test)")
    if dart_script:
        canary(dart_script)
    else:
        fail("canary: dart predicate not found")
    if analyze_script:
        canary_analyze(analyze_script)
    else:
        fail("canary[analyze]: analyze predicate not found")
    print()

    if FAILURES:
        print(f"RED — {len(FAILURES)} check(s) failed:")
        for m in FAILURES:
            print(f"  - {m.splitlines()[0]}")
        sys.exit(1)
    print("GREEN — every gate predicate parses and every fixture behaves.")

if __name__ == "__main__":
    main()
