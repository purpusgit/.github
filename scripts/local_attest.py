#!/usr/bin/env python3
"""LOCAL-ATTEST-1 (specs ci-git/spec-local-test-attestation.md): a session's full `flutter test` run lets CI skip the suite.

Two modes, one source of truth for every field, so the session and CI compute them the same way:

  curl -sL https://raw.githubusercontent.com/purpusgit/.github/main/scripts/local_attest.py -o /tmp/local_attest.py
  python3 /tmp/local_attest.py session [--base cwb]      (from the package root)
      In a session container. Refuses a dirty tree, builds the merge of HEAD with origin/<base> (the tree CI tests),
      runs the full VM suite there under a fixed environment, prints only the summary and the failures
      (READ-FAILURES-1), and on a clean pass writes build/local_attest/attestation.json. Then post it with the box
      helper (it posts as the bot; see the spec): run_shell `python3 /root/.orbit-bridge/attest_post.py <repo> <pr>`
      with the file's content on stdin.

  python3 tool/local_attest.py ci-check
      Inside the `flutter test` job, after `flutter pub get`. Reads the bot's newest attestation comment on the PR and
      prints skip=true to $GITHUB_OUTPUT only when every field equals what CI itself has. Anything else: skip=false,
      and the full suite runs as before. It never fails the job.

Fields: merge tree sha, Flutter framework/engine/Dart revisions, sha256 of the resolved pubspec.lock, sha256 of the
fixed environment, sha256 of the sorted VM suite list, the command. A run with any failure at a tree is never attested,
even if a later run at the same tree passes (flake rule, D1.7).
"""
import hashlib
import shutil, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request

VERSION = "v1"
MARKER = "<!-- local-test-attest v1 -->"
HINT_MARKER = "<!-- local-test-attest-hint -->"
BOT_LOGINS = {"orbit-sessions-bot[bot]"}
COMMAND = ["flutter", "test", "--reporter", "json"]
# The fixed environment both sides run under (D1.5). PATH/HOME/PUB_CACHE are passed through but not hashed.
FIXED_ENV = {}   # built per repo by fixed_env(): only what the repo's own CI test job pins (JAPA-D01, 26 Nov: an env CI
                 # does not use made 71 Japa tests fail locally that pass in CI, so no PR could attest)


def test_tz(root="."):
    """The time zone the repo's CI test job pins (`TZ:` in a workflow), else the runners' own (IST). Japa pins UTC."""
    import glob
    for f in sorted(glob.glob(os.path.join(root, ".github", "workflows", "*.y*ml"))):
        m = re.search(r"^\s+TZ:\s*['\"]?([A-Za-z_/+-]+)", open(f, encoding="utf-8", errors="replace").read(), re.M)
        if m:
            return m.group(1)
    return "Asia/Kolkata"
STATE_DIR = os.path.expanduser("~/.orbit-attest")


def sh(cmd, cwd=None, check=True, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        sys.exit(f"local_attest: {' '.join(cmd)} failed: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def sha256_file(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _workflow_text(root="."):
    import glob
    return "\n".join(open(f, encoding="utf-8", errors="replace").read() for f in sorted(glob.glob(os.path.join(root, ".github", "workflows", "*.y*ml"))))


def fixed_env(root="."):
    """The environment the repo's CI test job pins, and nothing more: TZ always (the runners' IST if unpinned);
    WEB_VISUAL_SHOTS and LANG only when a workflow sets them."""
    t = _workflow_text(root)
    env = dict(FIXED_ENV, TZ=test_tz(root))
    if re.search(r"^\s+WEB_VISUAL_SHOTS:\s*['\"]?1", t, re.M):
        env["WEB_VISUAL_SHOTS"] = "1"
    m = re.search(r"^\s+LANG:\s*['\"]?([A-Za-z_.0-9-]+)", t, re.M)
    if m:
        env["LANG"] = m.group(1)
    return env


def env_sha(root="."):
    return hashlib.sha256(json.dumps(fixed_env(root), sort_keys=True).encode()).hexdigest()


def sdk(root):
    v = json.loads(sh(["flutter", "--version", "--machine"], cwd=root))
    return {"framework": v.get("frameworkRevision"), "engine": v.get("engineRevision"), "dart": v.get("dartSdkVersion")}


TESTON = re.compile(r"""@TestOn\(\s*['"]([^'"]+)['"]\s*\)""")


def vm_suites(root):
    """Every *_test.dart under test/ that runs on the VM: a file whose @TestOn excludes the VM is left out."""
    out = []
    for d, _, files in os.walk(os.path.join(root, "test")):
        for f in files:
            if not f.endswith("_test.dart"):
                continue
            p = os.path.relpath(os.path.join(d, f), root)
            head = open(os.path.join(root, p), encoding="utf-8", errors="replace").read(4000)
            m = TESTON.search(head)
            if m and "vm" not in m.group(1) and ("browser" in m.group(1) or "chrome" in m.group(1) or "js" in m.group(1)):
                continue
            out.append(p.replace(os.sep, "/"))
    return sorted(out)


def suite_sha(paths):
    return hashlib.sha256("\n".join(paths).encode()).hexdigest()


# Generated translations (every locale but English) are left out of the code fingerprint: the L10N auto-translate bot
# commits them to the PR after the session's run, and they change no Dart (outside CI review, 25 Nov: ~120 min/day).
I18N_SKIP = re.compile(r"(^|/)assets/translations/(?!en_US\.json$)[^/]+\.json$")


def code_sha(root, tree):
    """sha256 over the tree's file list (path + blob sha), without generated translations."""
    out = sh(["git", "ls-tree", "-r", tree], cwd=root)
    keep = [l for l in out.splitlines() if not I18N_SKIP.search(l.split("\t", 1)[-1])]
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()


def fields(root, tree):
    return {
        "version": VERSION,
        "merge_tree": tree,
        "code_sha256": code_sha(root, tree),
        "sdk": sdk(root),
        "lock_sha256": sha256_file(os.path.join(root, "pubspec.lock")),
        "env_sha256": env_sha(root),
        "suite_sha256": suite_sha(vm_suites(root)),
        "command": " ".join(COMMAND),
    }


def key_of(f):
    return hashlib.sha256(json.dumps({k: f[k] for k in ("code_sha256", "sdk", "lock_sha256", "env_sha256", "suite_sha256", "command")}, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- session mode
def session(base):
    root = os.getcwd()
    sh(["git", "rev-parse", "--show-toplevel"])
    if sh(["git", "status", "--porcelain"], cwd=root):
        sys.exit("local_attest: the tree has uncommitted changes. Commit first: only a committed tree can be attested.")
    head = sh(["git", "rev-parse", "HEAD"], cwd=root)
    sh(["git", "fetch", "-q", "--depth=50", "origin", base], cwd=root)
    r = subprocess.run(["git", "merge-tree", "--write-tree", "HEAD", "FETCH_HEAD"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("local_attest: HEAD does not merge cleanly with origin/%s. Rebase first; there is nothing to attest." % base)
    # The PR HEAD's tree is what is attested (IAC-00, 26 Nov, outside audit): a merge-tree hash was voided by every merge
    # to the base (about one an hour), so no attestation survived to CI. The base still has to merge cleanly; the
    # post-merge push run stays the integration check.
    tree = sh(["git", "rev-parse", "HEAD^{tree}"], cwd=root)
    base_sha = sh(["git", "rev-parse", "FETCH_HEAD"], cwd=root)
    work = tempfile.mkdtemp(prefix="local-attest-")
    sh(["git", "worktree", "add", "-q", "--detach", work, head], cwd=root)
    try:
        sh(["flutter", "pub", "get"], cwd=work)
        f = fields(work, tree)
        os.makedirs(STATE_DIR, exist_ok=True)
        fenv = fixed_env(work)
        # The flaky guard counts runs of this tree under THIS test env only: a run under another env
        # (e.g. before .github#164 matched the env to CI) is a different experiment, not a flake.
        ekey = hashlib.sha256(json.dumps(fenv, sort_keys=True).encode()).hexdigest()[:12]
        hist = os.path.join(STATE_DIR, f"{tree}.{ekey}.log")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "PUB_CACHE", "FLUTTER_ROOT") if k in os.environ}
        env.update(fenv)
        t0 = time.time()
        rep = os.path.join(work, "build", "local_attest_report.json")
        os.makedirs(os.path.dirname(rep), exist_ok=True)
        with open(rep, "w") as out:
            rc = subprocess.run(COMMAND, cwd=work, stdout=out, stderr=subprocess.DEVNULL, env=env).returncode
        secs = int(time.time() - t0)
        passed, failed, skipped, loaded, names, errors = parse_report(rep)
        ok = rc == 0 and failed == 0 and errors == 0
        with open(hist, "a") as h:
            h.write(json.dumps({"at": int(time.time()), "ok": ok, "passed": passed, "failed": failed}) + "\n")
        print(f"local_attest: {passed} passed, {failed} failed, {skipped} skipped, {len(loaded)} files, {secs}s")
        for n in names[:40]:
            print("  FAILED: " + n)
        if not ok:
            sys.exit("local_attest: not attested (a failure). Fix it, commit, and run again: the fix is a new tree.")
        hist_runs = [json.loads(l) for l in open(hist)]
        if any(not x["ok"] for x in hist_runs):
            sys.exit("local_attest: not attested: an earlier run at this exact tree failed (flaky?). Change the code or let CI run the suite.")
        want = vm_suites(work)
        missing = [p for p in want if p not in loaded]
        if missing:
            sys.exit(f"local_attest: not attested: {len(missing)} suite files did not load, e.g. {missing[:3]}")
        f.update({"head_sha": head, "base_sha": base_sha, "passed": passed, "skipped": skipped, "seconds": secs,
                  "files": len(loaded), "wrapper_sha": sha256_file(os.path.abspath(__file__)), "key": None})
        f["key"] = key_of(f)
        outdir = os.path.join(root, "build", "local_attest")
        os.makedirs(outdir, exist_ok=True)
        open(os.path.join(outdir, "attestation.json"), "w").write(json.dumps(f, indent=1))
        print(f"local_attest: attested. Post it: {os.path.join(outdir, 'attestation.json')}")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=root, capture_output=True)


def parse_report(path):
    passed = failed = skipped = errors = 0
    loaded, names, tests = set(), [], {}
    suites = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t == "suite":
            suites[e["suite"]["id"]] = e["suite"].get("path")
        elif t == "testStart":
            tests[e["test"]["id"]] = e["test"]
        elif t == "error":
            errors += 1
            names.append(tests.get(e.get("testID"), {}).get("name", "?") + " (error)")
        elif t == "testDone" and not e.get("hidden"):
            tt = tests.get(e["testID"], {})
            p = suites.get(tt.get("suiteID"))
            if p:
                loaded.add(os.path.relpath(p, os.getcwd()) if os.path.isabs(p) else p)
            if e.get("skipped"):
                skipped += 1
            elif e.get("result") == "success":
                passed += 1
            else:
                failed += 1
                names.append(tt.get("name", "?"))
    # suite paths can be absolute: normalise to test/...
    norm = set()
    for p in loaded:
        i = p.replace(os.sep, "/").find("test/")
        norm.add(p[i:] if i >= 0 else p)
    return passed, failed, skipped, norm, names, errors


# ---------------------------------------------------------------- ci-check mode
def gh(url, token, method="GET", data=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data else None,
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def out(skip, why):
    print(("ACCEPTED local attestation: " if skip else "no usable local attestation: ") + why)
    go = os.environ.get("GITHUB_OUTPUT")
    if go:
        open(go, "a").write(f"skip={'true' if skip else 'false'}\n")
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        open(s, "a").write(("**Local attestation accepted:** " if skip else "Local attestation: not used: ") + why + "\n")


def head_worktree(head_sha):
    """A worktree of the PR head commit (CI checks out the merge commit); fetched if the shallow clone lacks it."""
    cwd = os.getcwd()
    if subprocess.run(["git", "cat-file", "-e", head_sha + "^{commit}"], cwd=cwd, capture_output=True).returncode != 0:
        sh(["git", "fetch", "-q", "--depth=1", "origin", head_sha], cwd=cwd)
    work = tempfile.mkdtemp(prefix="local-attest-ci-")
    sh(["git", "worktree", "add", "-q", "--detach", work, head_sha], cwd=cwd)
    # Package repos often do not commit pubspec.lock: use the one this job's `pub get` resolved (the session hashes the
    # lock its own pub get resolved, so a sibling that moved in between shows up as a lock mismatch, never a crash).
    for f in ("pubspec.lock",):
        if not os.path.exists(os.path.join(work, f)) and os.path.exists(os.path.join(cwd, f)):
            shutil.copy(os.path.join(cwd, f), os.path.join(work, f))
    return work


def ci_check():
    if (os.environ.get("CI_TRUST_LOCAL") or "").lower() == "off":
        return out(False, "CI_TRUST_LOCAL is off")
    ev = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
    pr = ev.get("pull_request")
    if not pr:
        return out(False, "not a pull request (pushes and nightly runs always run the full suite)")
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ.get("GH_TOKEN", "")
    api = f"https://api.github.com/repos/{repo}"
    # Primary path, no token needed: the box helper also writes the attestation to the public purpusgit/.github repo,
    # branch local-attest, att/<repo>/<merge tree>.json (only the bot can write there). Most package workflows grant
    # the job token `contents: read` only, which cannot read PR comments or statuses.
    # Compare against the PR head's tree (what the session attests), read in a worktree of the head commit.
    root = head_worktree(pr["head"]["sha"])
    tree = sh(["git", "rev-parse", "HEAD^{tree}"], cwd=root)
    name = repo.split("/")[-1]
    have_code = code_sha(root, tree)
    try:
        with urllib.request.urlopen(f"https://raw.githubusercontent.com/purpusgit/.github/local-attest/att/{name}/{have_code}.json", timeout=20) as r:
            a = json.load(r)
        have = fields(root, tree)
        bad = [k for k in ("version", "code_sha256", "sdk", "lock_sha256", "env_sha256", "suite_sha256", "command") if a.get(k) != have[k]]
        if not bad and a.get("key") == key_of(have):
            return out(True, f"{a.get('passed')} passed in the session, same tree/SDK/deps/env/suite (att/{name}/{have_code[:12]})")
        return out(False, f"`{bad[0] if bad else 'key'}` differs from the posted attestation")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return out(False, f"could not read the attestation store ({e.code})")
    except Exception as e:
        return out(False, f"could not read the attestation store ({type(e).__name__})")
    try:
        comments, page = [], 1
        while True:
            c = gh(f"{api}/issues/{pr['number']}/comments?per_page=100&page={page}", token)
            comments += c
            if len(c) < 100:
                break
            page += 1
    except Exception as e:
        return out(False, f"could not read PR comments ({type(e).__name__})")
    att = [c for c in comments if MARKER in (c.get("body") or "") and c["user"]["login"] in BOT_LOGINS]
    if not att:
        hint(api, token, pr, comments)
        return out(False, "none posted")
    c = att[-1]
    if c["created_at"] != c["updated_at"]:
        return out(False, "the newest attestation comment was edited")
    m = re.search(r"```json\n(.*?)\n```", c["body"], re.S)
    try:
        a = json.loads(m.group(1))
    except Exception:
        return out(False, "the attestation comment has no readable JSON")
    have = fields(root, tree)
    for k in ("version", "code_sha256", "sdk", "lock_sha256", "env_sha256", "suite_sha256", "command"):
        if a.get(k) != have[k]:
            return out(False, f"`{k}` differs (attested {str(a.get(k))[:16]}, CI {str(have[k])[:16]})")
    if a.get("key") != key_of(have):
        return out(False, "key mismatch")
    try:
        st = gh(f"{api}/commits/{pr['head']['sha']}/statuses?per_page=100", token)
        if not any(s.get("context") == "local-attest/v1" and s.get("state") == "success" and a["key"][:16] in (s.get("description") or "") for s in st):
            return out(False, "no matching local-attest/v1 commit status on the head")
    except Exception as e:
        return out(False, f"could not read commit statuses ({type(e).__name__})")
    out(True, f"{a.get('passed')} passed in the session, same tree/SDK/deps/env/suite (comment {c['html_url']})")


def hint(api, token, pr, comments):
    if any(HINT_MARKER in (c.get("body") or "") for c in comments):
        return
    body = (HINT_MARKER + "\n**Skip the 14-minute `flutter test` next time (LOCAL-ATTEST-1).** Before marking this PR ready, "
            "run the full suite in your own container: `curl -sL https://raw.githubusercontent.com/purpusgit/.github/main/scripts/local_attest.py -o /tmp/local_attest.py && python3 /tmp/local_attest.py session`, then post the file it writes "
            "with the box helper (`python3 /root/.orbit-bridge/attest_post.py <repo> <pr>`, through the sandbox owner "
            "connector). CI then runs a 10% spot-check instead of the whole suite. Read only the summary and failures "
            "(READ-FAILURES-1). Spec: specs `ci-git/spec-local-test-attestation.md`.")
    try:
        gh(f"{api}/issues/{pr['number']}/comments", token, "POST", {"body": body})
    except Exception:
        pass


def spot_list(seed, frac=0.10):
    """The files the blocking spot-check runs: the width harness (it writes the ux-critic inputs) plus ~10% at random."""
    import random
    root = os.getcwd()
    allf = vm_suites(root)
    rnd = random.Random(seed)
    k = max(1, int(len(allf) * frac))
    pick = set(rnd.sample(allf, k))
    pick.update(p for p in allf if p.startswith("test/UI/web_visual/"))
    pick.add("test/UI/web_visual/web_width_test.dart")
    print(" ".join(sorted(p for p in pick if os.path.exists(p))))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "session":
        base = sys.argv[sys.argv.index("--base") + 1] if "--base" in sys.argv else "cwb"
        session(base)
    elif mode == "ci-check":
        ci_check()
    elif mode == "spot-list":
        spot_list(sys.argv[2] if len(sys.argv) > 2 else "0")
    else:
        sys.exit(__doc__)
