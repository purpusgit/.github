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
  5. Runs actionlint over EVERY workflow in this repo — the workflow-validity
     class that steps 1-2 structurally cannot see, because a workflow Actions
     refuses to compile is still perfectly valid YAML. See actionlint_gate().

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
#   'consolidated' -> a gate whose steps are byte-identical COPIES of predicates
#               that already live in this directory: assert the equality (that is
#               the coverage — each source predicate has its own fixture above),
#               then red-proof one named step against a real pass/fail fixture
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
    # A2 consolidation, live on pkg_orbit_japa and pkg_orbit_client_core ONLY
    # (fleet-wide A2 stays deferred). This gate holds no logic of its own: each of
    # its seven mirrored steps is a BYTE-IDENTICAL copy of a predicate above, so
    # that one billed job can do what four to seven separately-billed jobs did.
    # `mirrors` asserts that equality, which is what makes the copies safe: the
    # moment either side is edited alone, THIS harness goes red and names the pair.
    # Equality IS the behavioural coverage here — every mirrored predicate already
    # has its own fixture in this same map, and re-running them under a second name
    # would assert nothing new. `redproof` still runs one of them against the real
    # dart pass/fail fixture so this entry can never be green on a gate that has
    # stopped catching anything.
    # NOT mirrored, and deliberately so: the exact-pin, A1 and contrast steps have
    # no reusable in THIS repo to compare against — their source of record is the
    # consumer repo's own workflow — so no equality is assertable and none is
    # claimed. They are still bash -n'd and actionlint'd by the generic loop.
    "reusable-consolidated-gates.yml": {"kind": "consolidated",
        "mirrors": {
            "Rule 24 — colours safety (no hardcoded Colors.* additions)":
                ("reusable-colors-safety-gate.yml", "Check for hardcoded colour additions"),
            "Dart escaped string interpolation check":
                ("reusable-dart-safety-gate.yml", "Check for escaped string interpolation"),
            "Rule 66 — barrel & factory safety":
                ("reusable-barrel-safety-gate.yml", "Detect barrel files and check for removals"),
            "Rule 84 — base URLs live only in client_core flavor_config.dart":
                ("reusable-rule84-flavor-fork-gate.yml", "Base URLs live only in client_core flavor_config.dart"),
            "Rule 84 — no base-URL fallback (a missing URL must be a compile error)":
                ("reusable-rule84-flavor-fork-gate.yml", "No base-URL fallback (a missing URL must be a compile error)"),
            "Rule 84 — a flavor must not own another package's screens":
                ("reusable-rule84-flavor-fork-gate.yml", "A flavor must not own another package's screens"),
        },
        "unmirrored": "exact-pin / A1 / contrast steps: source of record is the consumer repo, not this one",
        "redproof": ("Dart escaped string interpolation check", "dart")},
    # `step`, not `dir`: the dir branch does not forward `expect`, and without one a
    # Python traceback exiting 1 scores as "violation caught". The predicate is pure
    # Python over a directory — no secrets, no network, no DB — so it earns a real
    # behavioural fixture rather than a SKIP. `expect` is verbatim gate output.
    "reusable-drizzle-journal-gate.yml": {"kind": "step",
        "step": "Check every .sql file has a matching journal entry",
        "key": "drizzle-journal",
        "expect": "ERROR: Drizzle journal completeness gate FAILED"},
    "reusable-taxo-lint.yml":        {"kind": None, "reason": "delegates to scripts/taxo_lint.py; --data needs live MySQL secrets"},
    "reusable-sql-execution-gate.yml": {"kind": None, "reason": "delegates to a per-repo harness (ts-node) run against a live MySQL service container; its exit code cannot be asserted here without a DB and the consumer repo's harness. bash -n + actionlint still cover its run: scripts."},
    "reusable-taxo-contract-lint.yml": {"kind": "dead", "reason": "0 callers org-wide (independently confirmed via a sweep of all 59 repos / 218 workflow files, 2026-08-13) -- not yet wired to any consumer. checker.py offline-tested by taxo-contract/test_checker.py."},
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

def build_barrel_repo(tmp, fpath, base_content, work_content, origin_ref=True,
                      extras=None):
    """Two-branch git repo where work branch overwrites fpath with work_content.
    Supports testing both removal (fail) and addition (pass) cases.

    base_content=None -> fpath does not exist on the base branch at all, i.e. a
    barrel file NEW in this PR. That is legitimate and must still pass; it is the
    case a naive "cat-file -e non-zero => fail" fix would have broken.
    origin_ref=False  -> refs/remotes/origin/cwb is never created, so the gate
    cannot resolve its base ref and must go RED.
    extras={path: content} -> extra files written IDENTICALLY on both branches,
    so they contribute no diff. Used to plant a real barrel alongside a
    non-barrel `index.dart`, which is the only way to tell "the gate correctly
    ignored the class file" apart from "the gate found nothing to check at all"."""
    g = ["git", "-c", "user.email=t@t.co", "-c", "user.name=t"]
    def run(*a): subprocess.run(list(a), cwd=tmp, check=True, capture_output=True)
    run(*g, "init", "-q", "-b", "cwb")
    full = os.path.join(tmp, fpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if base_content is None:
        open(os.path.join(os.path.dirname(full), "keep.txt"), "w").write("placeholder\n")
    else:
        open(full, "w").write(base_content)
    for xp, xc in (extras or {}).items():
        xfull = os.path.join(tmp, xp)
        xdir = os.path.dirname(xfull)
        if xdir:
            os.makedirs(xdir, exist_ok=True)
        open(xfull, "w").write(xc)
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
    # A real barrel, planted unchanged on both branches in the private-file case.
    # Without it an "ignored the private file" pass is indistinguishable from a
    # "found no barrels at all" pass — the gate short-circuits on an empty list.
    real_barrel = {"lib/pkg.dart": "export 'src/a.dart';\nexport 'src/b.dart';\n"}
    # A PRIVATE implementation file that happens to be named index.dart, losing
    # lines. This is pkg_orbit_binder/lib/src/utils/index.dart: the Binder class,
    # zero exports, under lib/src/ so no downstream can import it. Its name alone
    # used to make it a barrel, freezing every line and failing this REQUIRED
    # check on any legitimate refactor.
    klass_base = ("class Binder {\n  void a() {}\n  void b() {}\n  void c() {}\n}\n")
    klass_work = ("class Binder {\n  void a() {}\n}\n")
    # The PR #373 incident itself, and the case with zero coverage before now: a
    # PUBLIC factory class at lib/endpoints/*/index.dart. Zero export lines, so
    # an `^export `-based rule would wave it through — but Rule 66 forbidden
    # action #2 is removing a method from exactly this. Verified live: all 6 of
    # pkg_orbit_client_core's lib/endpoints/*/index.dart have 0 export lines and
    # hold *EndpointsFactory classes; org_service's holds the very three
    # getSpiritual*Endpoints methods Rule 66 cites.
    factory_base = ("class OrgServiceEndpointsFactory {\n"
                    "  SpiritualCauseEndpoints getSpiritualCauseEndpoints() => x;\n"
                    "  SpiritualUnitTypeEndpoints getSpiritualUnitTypeEndpoints() => y;\n}\n")
    factory_work = ("class OrgServiceEndpointsFactory {\n"
                    "  SpiritualCauseEndpoints getSpiritualCauseEndpoints() => x;\n}\n")
    cases = [
        # case, fpath, base_content, work_content, want_zero, expect, origin_ref, extras
        ("pass", fpath, base, "export 'a.dart';\nexport 'b.dart';\nexport 'c.dart';\n",
         True, None, True, None),
        ("fail", fpath, base, "export 'a.dart';\n", False, "RULE 66 VIOLATION", True, None),
        # R1 — barrel file absent at base: NEW in this PR, still a pass.
        ("pass[new-barrel-file-at-base]", fpath, None, base, True, None, True, None),
        # R1 — base ref unresolvable: no diff is computable, so RED.
        ("fail[base-ref-unresolvable]", fpath, base, "export 'a.dart';\n", False,
         "could not resolve base ref", False, None),
        # ── both directions of "protection follows reachability, not filename" ──
        # A: a PRIVATE lib/src/**/index.dart shedding lines must PASS, while a
        #    real barrel sits in the same tree (so the gate provably had
        #    something to check and did not just short-circuit on an empty list).
        ("pass[private-lib-src-index.dart-loses-lines]", "lib/src/utils/index.dart",
         klass_base, klass_work, True, None, True, real_barrel),
        # B: a PUBLIC nested barrel must STILL red when it sheds an export — the
        #    fix must not have bought A by switching the gate off.
        ("fail[public-nested-barrel-still-reds]", "lib/nested/index.dart",
         base, "export 'a.dart';\n", False, "RULE 66 VIOLATION", True, real_barrel),
        # C: a PUBLIC factory index.dart with ZERO exports shedding a METHOD must
        #    still red. This is the PR #373 incident and the case an
        #    `^export `-based rule would have silently let through.
        ("fail[public-factory-method-removal-still-reds]", "lib/endpoints/org_service/index.dart",
         factory_base, factory_work, False, "RULE 66 VIOLATION", True, real_barrel),
    ]
    for case, fp, base_content, work_content, want_zero, expect, origin_ref, extras in cases:
        with tempfile.TemporaryDirectory() as tmp:
            build_barrel_repo(tmp, fp, base_content, work_content,
                              origin_ref=origin_ref, extras=extras)
            _assert_diff(script, label, case, want_zero, tmp, expect=expect)

def behavioural_consolidated(named, beh, label):
    """Assert every mirrored step is byte-identical to its source predicate, then
    red-proof one of them against a real fixture.

    A reusable WORKFLOW cannot be `uses:`d as a STEP, so a job that consolidates
    several gates to save billed minutes has no choice but to copy their `run:`
    blocks. Copies drift. This is what stops them: the equality is asserted against
    the LIVE source predicate on every PR to this repo, so editing one side alone
    reds here and says which pair disagrees. Nothing is duplicated silently.
    """
    for step_name, (src_file, src_step) in beh["mirrors"].items():
        mine = named.get(step_name)
        if mine is None:
            fail(f"{label}: mirrored step {step_name!r} no longer exists in this gate — "
                 "repoint or remove the mirror; do NOT let it silently stop asserting.")
            continue
        src_path = os.path.join(WF_DIR, src_file)
        if not os.path.exists(src_path):
            fail(f"{label}: mirror source {src_file} is gone — the consolidated copy is "
                 "now the only copy and nothing can check it.")
            continue
        theirs = extract_named_runs(load_yaml(src_path)).get(src_step)
        if theirs is None:
            fail(f"{label}: source step {src_step!r} no longer exists in {src_file} — "
                 "the single-purpose gate changed shape; repoint this mirror.")
        elif mine != theirs:
            fail(f"{label}: {step_name!r} has DRIFTED from {src_file}[{src_step!r}] — "
                 "the consolidated copy and the single-purpose gate no longer agree. "
                 "Edit both in one commit.")
        else:
            ok(f"{label}: {step_name!r} is byte-identical to {src_file}[{src_step!r}]")
    if beh.get("unmirrored"):
        print(f"  ⤳ PARTIAL: no equality assertion possible for — {beh['unmirrored']}")
    rp_step, rp_key = beh["redproof"]
    script = named.get(rp_step)
    if script is None:
        fail(f"{label}: red-proof step {rp_step!r} no longer exists — this entry would "
             "be green on a gate proven to catch nothing.")
    else:
        behavioural_dir(script, rp_key, label + "[red-proof]")

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

# ── actionlint: workflow-validity check ──────────────────────────────────────
# Added 2026-08-10 after notify-host-tip-moved.yml was fanned out to 13 repos
# carrying a literal empty `${{ }}` inside what reads as a shell comment in a
# `run:` body. Actions evaluates expressions everywhere in a run: block scalar —
# `#` means nothing to it — and an empty expression cannot compile, so Actions
# REJECTED the whole workflow at validation time.
#
# Nothing in this harness could see it. The file is valid YAML: yaml.safe_load
# parses it clean, so load_yaml() and bash_n() both pass it. And the symptom on
# GitHub is an absence, not a red step: a workflow that fails validation never
# gets its trigger filter evaluated, so a run is materialised for EVERY push
# regardless of `on: push: branches:`, then dies in 0s with 0 jobs, a 404
# logs_url and the file PATH reported where the `name:` should be. It went
# unnoticed for days because there was nothing to read.
#
# actionlint parses GitHub's expression grammar, which is the one thing
# yaml.safe_load and `bash -n` structurally cannot do.
ACTIONLINT_FLAGS = ["-no-color", "-oneline", "-shellcheck=", "-pyflakes="]
# NARROWED, deliberately. The two empty flags disable actionlint's shellcheck
# and pyflakes integrations. On this tree at c8b4cf4 they contribute four
# pre-existing style/info findings — SC2005 (colors gate), SC2016 (dart gate),
# SC2086 (rule84 gate), SC2001 (sql-typestring gate) — none of which this PR
# introduced, and silencing them means editing live predicates that every
# consumer repo resolves `@main`. Turning them on would red this harness on day
# one for reasons no PR caused, which is how a gate gets disabled by whoever is
# unblocking a release at 2am.
#   What the exclusion costs: no shell-quoting lint (SC2086-class unquoted
#   expansion) and no lint of embedded python heredocs beyond the py_compile
#   above. What it does NOT cost: anything in the expression/syntax class — that
#   is actionlint's own parser, unaffected by these flags, and the fixture below
#   pins exactly that. Re-enable by clearing the two flags once the four are
#   fixed in their own PR.

# The defective line, verbatim from notify-host-tip-moved.yml @ 6fba4248 (the
# fan-out commit). Copied rather than paraphrased on purpose: a fixture that
# tests a hand-written approximation of a bug proves nothing about the bug.
BROKEN_EXPR_LINE = (
    "          # $GITHUB_REPOSITORY / $GITHUB_SHA rather than `${{ }}` interpolation —"
)
FIXED_EXPR_LINE = BROKEN_EXPR_LINE.replace("`${{ }}` interpolation",
                                           "expression interpolation")
ACTIONLINT_FIXTURE = """name: Notify host — tip moved
on:
  push:
    branches: [cwb]
permissions: {}
jobs:
  notify:
    name: dispatch package-tip-moved
    runs-on: ubuntu-latest
    steps:
      - name: Dispatch to main_org_orbit
        run: |
          set -euo pipefail
__LINE__
          # nothing untrusted gets spliced into the script text.
          echo "dispatched: $GITHUB_REPOSITORY @ $GITHUB_SHA"
"""


def _actionlint(exe, paths, cwd):
    r = subprocess.run([exe] + ACTIONLINT_FLAGS + list(paths),
                       cwd=cwd, text=True, capture_output=True)
    return r.returncode, r.stdout + r.stderr


def actionlint_gate():
    """Lint every workflow in THIS repo, and prove the linter can still go red.

    Fails closed: if actionlint is absent or unrunnable this reds. An absent
    check is precisely the failure mode the incident above consisted of, so
    'could not run the linter' must never score as 'the linter found nothing'.
    """
    print("── actionlint (workflow validity: expression + syntax)")
    exe = shutil.which("actionlint")
    if not exe:
        fail("actionlint: not installed / not on PATH — refusing to score an "
             "un-run linter as green (see the Install actionlint step in "
             "self-test-gates.yml)")
        return

    # Red-proof first: if the fixture does not behave, the clean run below means
    # nothing. Same reasoning as the dart canary — a check that cannot fail
    # proves nothing.
    with tempfile.TemporaryDirectory() as tmp:
        wfd = os.path.join(tmp, ".github", "workflows")
        os.makedirs(wfd)
        cases = [("fixture-broken.yml", BROKEN_EXPR_LINE),
                 ("fixture-control.yml", FIXED_EXPR_LINE)]
        for fn, line in cases:
            with open(os.path.join(wfd, fn), "w") as f:
                f.write(ACTIONLINT_FIXTURE.replace("__LINE__", line))
        code, out = _actionlint(exe, [os.path.join(wfd, "fixture-broken.yml")], tmp)
        if code == 0:
            fail("actionlint[fixture]: the real defect shape (empty `${{ }}` in a "
                 "run: comment) was NOT caught — this actionlint no longer detects "
                 "the incident class")
        elif "[expression]" not in out:
            fail(f"actionlint[fixture]: exited {code} but reported no [expression] "
                 f"finding — red for the WRONG reason\n{out}")
        else:
            ok("actionlint[fixture]: empty `${{ }}` inside a run: comment goes RED "
               "[expression] — the real defect shape is caught")
        code, out = _actionlint(exe, [os.path.join(wfd, "fixture-control.yml")], tmp)
        if code != 0:
            fail(f"actionlint[fixture]: the control (same file, empty expression "
                 f"removed) is not clean — the fixture reds for an unrelated "
                 f"reason and pins nothing\n{out}")
        else:
            ok("actionlint[fixture]: same file with the expression removed is GREEN")

    # Scope: every workflow on disk, derived at runtime. Never a hardcoded list —
    # a list is how a file gets added and stays invisible to its own gate.
    # self-test-gates.yml is INCLUDED here (main() excludes it from the gate loop
    # because it is not a gate; it is still a workflow that can fail validation).
    wfs = sorted(glob.glob(os.path.join(WF_DIR, "*.yml")) +
                 glob.glob(os.path.join(WF_DIR, "*.yaml")))
    if not wfs:
        fail("actionlint: derived zero workflow files from "
             f"{WF_DIR} — scope derivation is broken, not the repo empty")
        return
    code, out = _actionlint(exe, wfs, ROOT)
    if code != 0:
        n = len([l for l in out.splitlines() if l.strip()])
        fail(f"actionlint: {n} finding(s) across {len(wfs)} workflow(s)\n{out}")
    else:
        ok(f"actionlint: all {len(wfs)} workflow(s) clean")
    print()


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
        elif beh["kind"] == "consolidated":
            behavioural_consolidated(named, beh, fn)
        print()

    actionlint_gate()

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
