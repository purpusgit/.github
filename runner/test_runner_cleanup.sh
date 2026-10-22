#!/bin/bash
# Self-check for runner-cleanup.sh (T415 B3). Uses only throwaway sleeps under a temp dir and a temp HOME.
set -u
here=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d); W=$T/_work; mkdir -p "$W/repo" "$T/home/.npm/_cacache" "$T/out" "$T/bin"
cp "$(command -v bash)" "$T/bin/Runner.Worker"                 # a stand-in live job: comm is Runner.Worker
fail=0; check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
alive() { kill -0 "$1" 2>/dev/null; }

# orphan: cwd under _work, reparented away from its (exited) parent
( cd "$W/repo" && exec setsid sleep 300 ) & sleep 0.2
ORPHAN=$(pgrep -n -f "^sleep 300$" -U "$(id -u)")
# live job: a child of Runner.Worker with cwd under _work
"$T/bin/Runner.Worker" -c "cd '$W/repo' && sleep 301; true" & sleep 0.2
LIVE=$(pgrep -n -f "^sleep 301$" -U "$(id -u)")
# outside _work: an orphan elsewhere
( cd "$T/out" && exec setsid sleep 302 ) & sleep 0.2
OUTSIDE=$(pgrep -n -f "^sleep 302$" -U "$(id -u)")

HOME="$T/home" RUNNER_CLEANUP_WORK="$W" RUNNER_CLEANUP_GRACE=1 bash "$here/runner-cleanup.sh"; rc=$?
sleep 0.5
check "hook exits 0"                       "[ $rc -eq 0 ]"
check "orphan under _work reaped"          "! alive $ORPHAN"
check "live job's child spared"            "alive $LIVE"
check "process outside _work spared"       "alive $OUTSIDE"
check "npm cache cleared in HOME"          "[ ! -d '$T/home/.npm/_cacache' ]"
# without a Runner.Worker ancestor and no override, the hook must reap nothing
( cd "$W/repo" && exec setsid sleep 303 ) & sleep 0.2
O2=$(pgrep -n -f "^sleep 303$" -U "$(id -u)")
HOME="$T/home" bash "$here/runner-cleanup.sh" >/dev/null
check "no Runner.Worker ancestor: nothing reaped" "alive $O2"

kill "$LIVE" "$OUTSIDE" "$O2" 2>/dev/null; pkill -f "^$T/bin/Runner.Worker" 2>/dev/null; rm -rf "$T"
[ $fail -eq 0 ] && echo "runner-cleanup self-check GREEN" || { echo "runner-cleanup self-check RED"; exit 1; }
