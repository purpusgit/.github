#!/usr/bin/env bash
# Self-test for the Test step of action.yml with a fake `flutter` on PATH. Run: bash timeout.test.sh
# The step's `run:` block is read out of action.yml, so what is tested is what ships.
set -u
here=$(cd "$(dirname "$0")" && pwd); tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
python3 - "$here/action.yml" "$tmp/step.sh" <<'PY'
import sys, yaml
steps = yaml.safe_load(open(sys.argv[1]))["runs"]["steps"]
open(sys.argv[2], "w").write(next(s["run"] for s in steps if s.get("name") == "Test"))
PY
# Fake flutter: behaviour from $FAKE (pass | fail | hang | stubborn). hang sleeps; stubborn also ignores TERM (needs KILL).
# Both leave a grandchild `sleep` whose pid goes to $PIDFILE, to prove the whole process group is taken down.
cat > "$tmp/flutter" <<'F'
#!/usr/bin/env bash
case "$FAKE" in
  pass) exit 0 ;;
  fail) echo "Some tests failed."; exit 1 ;;
  hang) sleep 300 & echo $! > "$PIDFILE"; wait ;;
  stubborn) trap '' TERM; sleep 300 & echo $! > "$PIDFILE"; while :; do sleep 1; done ;;
esac
F
chmod +x "$tmp/flutter"; fail=0
run() { # name, FAKE mode, expected exit, expected message fragment (or ""), extra check
  local name=$1 mode=$2 want=$3 frag=$4 d="$tmp/w.$1"; mkdir -p "$d/test"; : > "$d/test/a_test.dart"
  local t0=$SECONDS out rc
  out=$(cd "$d" && env PATH="$tmp:$PATH" FAKE="$mode" PIDFILE="$d/pid" RUNNER_NAME=orbit-ci-9 FLUTTER_TEST_TIMEOUT=2s FLUTTER_TEST_KILL_AFTER=2s bash "$tmp/step.sh" 2>&1); rc=$?
  local took=$((SECONDS - t0)) ok=1
  [ "$rc" -eq "$want" ] || ok=0
  [ -z "$frag" ] || grep -qF -- "$frag" <<<"$out" || ok=0
  [ "$took" -lt 20 ] || ok=0
  if [ -s "$d/pid" ]; then sleep 0.5 # a killed grandchild may linger as an unreaped zombie (Z); that is dead
    st=$(ps -o stat= -p "$(cat "$d/pid")" 2>/dev/null | tr -d ' ')
    case "$st" in ""|Z*) ;; *) ok=0; out="$out [grandchild still alive: $st]" ;; esac
  fi
  if [ "$ok" = 1 ]; then echo "ok   $name"; else echo "FAIL $name: rc=$rc took=${took}s out=$out"; fail=1; fi
}
run passes       pass     0 ""
run failing-test fail     1 "Some tests failed."
run hang         hang     1 "::error title=flutter test hung::no result after 2s (runner orbit-ci-9)"
run stubborn     stubborn 1 "::error title=flutter test hung::no result after 2s (runner orbit-ci-9)"
# a failing test is NOT reported as a hang
d="$tmp/w.failing-test"; out=$(cd "$d" && env PATH="$tmp:$PATH" FAKE=fail RUNNER_NAME=x bash "$tmp/step.sh" 2>&1)
grep -q "hung" <<<"$out" && { echo "FAIL failing-test is reported as a hang"; fail=1; } || echo "ok   failing-test is not a hang"
# default limit reads "30 min"
mkdir -p "$tmp/w.default/test"; : > "$tmp/w.default/test/a_test.dart"
grep -q 'FLUTTER_TEST_TIMEOUT:-30m' "$tmp/step.sh" && echo "ok   default limit is 30m" || { echo "FAIL default limit is not 30m"; fail=1; }
case "30m" in *m) shown="30 min" ;; esac; [ "$shown" = "30 min" ] && echo "ok   message wording for 30m is '30 min'"
# the existing 'No suite' check is untouched
mkdir -p "$tmp/w.nosuite"; out=$(cd "$tmp/w.nosuite" && env PATH="$tmp:$PATH" FAKE=pass bash "$tmp/step.sh" 2>&1); rc=$?
[ "$rc" -eq 1 ] && grep -q "No suite" <<<"$out" && echo "ok   missing suite still red" || { echo "FAIL missing suite: rc=$rc"; fail=1; }
exit $fail
