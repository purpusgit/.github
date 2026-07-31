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
  3. For the four SELF-CONTAINED gates (no DB, no cross-repo token, single repo),
     runs the real extracted predicate against a PASS fixture (assert exit 0) and a
     FAIL fixture (assert exit != 0).
  4. Runs a CANARY: deliberately weakens the dart predicate and asserts the FAIL
     fixture stops catching it — proving the behavioural assertions genuinely
     exercise the predicate (a test that cannot fail proves nothing).

The four DELEGATING gates (taxo-lint, taxo-contract-lint, taxo-data-nightly,
host-pin-autobump) call external Python that needs live MySQL secrets, a
cross-repo checkout token, or opens a real PR against the host — their exit codes
cannot be asserted in isolation here, so they get syntax-checking only, logged
explicitly as SKIP-BEHAVIOURAL with the reason. We do not fake coverage.

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
#   'colors' -> build a two-branch git repo at runtime (gate is diff-based)
#   None     -> external: syntax-check only, with `reason`
BEHAVIOUR = {
    "reusable-dart-safety-gate.yml":            {"kind": "dir", "key": "dart"},
    "reusable-sql-safety-gate.yml":             {"kind": "dir", "key": "sql-semicolon"},
    "reusable-sql-typestring-safety-gate.yml":  {"kind": "dir", "key": "sql-typestring"},
    "reusable-colors-safety-gate.yml":          {"kind": "colors"},
    "reusable-taxo-lint.yml":        {"kind": None, "reason": "delegates to scripts/taxo_lint.py; --data needs live MySQL secrets"},
    "reusable-taxo-contract-lint.yml": {"kind": None, "reason": "cross-repo checkout (ORG_GITHUB_READ_TOKEN) + jsonschema + checker.py"},
    "taxo-data-lint-nightly.yml":    {"kind": None, "reason": "DB-backed scheduled job; needs TAXO_DB_* MySQL secrets"},
    "reusable-host-pin-autobump.yml": {"kind": None, "reason": "rewrites host pubspec, needs host PAT, opens a real PR against main_org_orbit"},
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

def behavioural_dir(script, key, label):
    src = os.path.join(FIXTURES, key)
    for case, want_zero in (("pass", True), ("fail", False)):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "repo")
            shutil.copytree(os.path.join(src, case), work)
            code, out = run_predicate(script, work)
            if want_zero and code != 0:
                fail(f"{label}: PASS fixture unexpectedly RED (exit {code})\n{out}")
            elif not want_zero and code == 0:
                fail(f"{label}: FAIL fixture was NOT caught (exit 0) — gate is asleep\n{out}")
            else:
                ok(f"{label}: {case} fixture behaves ({'exit 0' if want_zero else 'non-zero'})")

def build_colors_repo(tmp, violating_line):
    """Two-branch git repo the diff-based colours gate needs. Base branch 'cwb'
    is clean; the work branch adds `violating_line` to lib/w.dart. origin/cwb is
    faked with update-ref (the gate's checkout/fetch are separate steps, not part
    of the run: block we execute)."""
    g = ["git", "-c", "user.email=t@t.co", "-c", "user.name=t"]
    def run(*a): subprocess.run(list(a), cwd=tmp, check=True, capture_output=True)
    run(*g, "init", "-q", "-b", "cwb")
    os.makedirs(os.path.join(tmp, "lib"))
    open(os.path.join(tmp, "lib", "w.dart"), "w").write("class W {}\n")
    run(*g, "add", "-A"); run(*g, "commit", "-qm", "base")
    run(*g, "update-ref", "refs/remotes/origin/cwb", "HEAD")
    run(*g, "checkout", "-qb", "work")
    with open(os.path.join(tmp, "lib", "w.dart"), "a") as f:
        f.write(violating_line + "\n")
    run(*g, "add", "-A"); run(*g, "commit", "-qm", "work")

def behavioural_colors(script, label):
    for case, line, want_zero in (
        ("pass", "  final c = colorScheme.primary;", True),
        ("fail", "  final c = Colors.red;", False),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            build_colors_repo(tmp, line)
            code, out = run_predicate(script, tmp)
            if want_zero and code != 0:
                fail(f"{label}: PASS fixture unexpectedly RED (exit {code})\n{out}")
            elif not want_zero and code == 0:
                fail(f"{label}: FAIL fixture was NOT caught (exit 0) — gate is asleep\n{out}")
            else:
                ok(f"{label}: {case} fixture behaves ({'exit 0' if want_zero else 'non-zero'})")

def canary(dart_script):
    """Negative test of the test: weaken the dart predicate (strip its `exit 1`)
    and confirm the FAIL fixture stops being caught. If a weakened predicate were
    STILL caught, our fail-assertion isn't actually exercising the predicate."""
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

def main():
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(WF_DIR, "*.yml")))
    # the self-test workflow is not a gate — don't test it against itself
    files = [f for f in files if f != "self-test-gates.yml"]
    print(f"Found {len(files)} workflow(s) in .github/workflows/\n")

    dart_script = None
    for fn in files:
        print(f"── {fn}")
        doc = load_yaml(os.path.join(WF_DIR, fn))
        runs = extract_runs(doc)
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
        elif beh["kind"] is None:
            print(f"  ⤳ SKIP-BEHAVIOURAL: {beh['reason']}")
        elif beh["kind"] == "dir":
            behavioural_dir(runs[-1], beh["key"], fn)
            if beh["key"] == "dart":
                dart_script = runs[-1]
        elif beh["kind"] == "colors":
            behavioural_colors(runs[-1], fn)
        print()

    print("── canary (negative test of the test)")
    if dart_script:
        canary(dart_script)
    else:
        fail("canary: dart predicate not found")
    print()

    if FAILURES:
        print(f"RED — {len(FAILURES)} check(s) failed:")
        for m in FAILURES:
            print(f"  - {m.splitlines()[0]}")
        sys.exit(1)
    print("GREEN — every gate predicate parses and every fixture behaves.")

if __name__ == "__main__":
    main()
