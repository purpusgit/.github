#!/usr/bin/env bash
# Self-test for the runner disk alarm. Run: bash scripts/test_runner_disk_alarm.sh
#  1. The check step of runner-disk-alarm.yml, with a fake `df`: low, boundary, override, bad input, and that all three check jobs run the SAME script.
#  2. The dedupe in reusable-notify-inbox.yml, with a fake `curl` that serves the inbox's comments and records what is posted.
# Both `run:` blocks are read out of the YAML, so what is tested is what ships.
set -u
here=$(cd "$(dirname "$0")/.." && pwd); tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; fail=0
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

python3 -I - "$here" "$tmp" <<'PY'
import sys, yaml
here, tmp = sys.argv[1:3]
a = yaml.safe_load(open(f"{here}/.github/workflows/runner-disk-alarm.yml"))
checks = {k: next(s["run"] for s in j["steps"] if s.get("id") == "df") for k, j in a["jobs"].items() if k.startswith("check-")}
open(f"{tmp}/check.sh", "w").write(next(iter(checks.values())))
open(f"{tmp}/n_checks", "w").write(str(len(checks)))
open(f"{tmp}/same", "w").write("1" if len(set(checks.values())) == 1 else "0")
open(f"{tmp}/labels", "w").write(" ".join(a["jobs"][k]["runs-on"] for k in checks))
n = yaml.safe_load(open(f"{here}/.github/workflows/reusable-notify-inbox.yml"))
open(f"{tmp}/post.sh", "w").write(next(s["run"] for s in n["jobs"]["notify"]["steps"] if s.get("name") == "Post one line"))
PY

# ── 1. the check step ───────────────────────────────────────────────────────────────────────────────
[ "$(cat "$tmp/n_checks")" = 3 ] && [ "$(cat "$tmp/same")" = 1 ] && ok "three check jobs, byte-identical step" || bad "check jobs differ or are not 3"
[ "$(cat "$tmp/labels")" = "orbit-ci orbit-ci-light orbit-ci-heavy" ] && ok "one job per label group: $(cat "$tmp/labels")" || bad "label groups: $(cat "$tmp/labels")"
cat > "$tmp/df" <<'F'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/vdb 191889408 0 %s 99%% %s\n' "${FAKE_DF_KB:-52428800}" "${FAKE_MOUNT:-/home/runner/actions-runner-3/_work}"
F
chmod +x "$tmp/df"
check() { # name, FAKE_DF_KB, MIN_GB, expected low (or ERR), then a note fragment
  local name=$1 kb=$2 min=$3 want=$4 frag=${5:-}
  : > "$tmp/out"; local o rc
  o=$(env PATH="$tmp:$PATH" FAKE_DF_KB="$kb" MIN_GB="$min" GROUP=orbit-ci-heavy RUNNER_NAME=orbit-ci-3 GITHUB_WORKSPACE="$tmp" GITHUB_OUTPUT="$tmp/out" bash "$tmp/check.sh" 2>&1); rc=$?
  if [ "$want" = ERR ]; then [ "$rc" -ne 0 ] && ok "$name" || bad "$name: expected an error, rc=0"; return; fi
  [ "$rc" -eq 0 ] && grep -qx "low=$want" "$tmp/out" && { [ -z "$frag" ] || grep -qF -- "$frag" "$tmp/out"; } && ok "$name" || bad "$name: rc=$rc out=$(cat "$tmp/out") log=$o"
}
check "50 GB free is not low"                   52428800 10  false
check "9.9 GB free is low"                      10380902 10  true  "runner orbit-ci-3 (orbit-ci-heavy): 9.9 GB free on /home/runner/actions-runner-3/_work, alarm under 10 GB"
check "exactly 10 GB free is not low"          10485760 10  false
check "the threshold is an input (50 GB < 100)" 52428800 100 true
check "a non-numeric threshold is an error"     52428800 abc ERR
check "an empty threshold is an error"          52428800 ""  ERR
check "the runner is exported for the dedupe"   52428800 10  false "runner=orbit-ci-3"
FAKE_MOUNT="/mount with space" check "a mount with a space is kept whole" 1048576 10 true "on /mount with space,"

# ── 2. the dedupe in the reusable notify workflow ──────────────────────────────────────────────────
cat > "$tmp/curl" <<'F'
#!/usr/bin/env bash
# fake curl: GET of /comments serves $FAKE_COMMENTS; POST copies the --data @file to $POSTED
url=${@: -1}; method=GET; data=
while [ $# -gt 0 ]; do case "$1" in -X) method=$2; shift;; --data) data=${2#@}; shift;; esac; shift; done
if [ "$method" = POST ]; then cp "$data" "$POSTED"; echo '{}'; else cat "$FAKE_COMMENTS"; fi
F
chmod +x "$tmp/curl"
now() { date -u -d "-$1 hours" +%Y-%m-%dT%H:%M:%SZ; }
post() { # name, key, hours, comments-json, expect posted(1/0), expect marker in body(1/0)
  local name=$1 key=$2 hrs=$3 comments=$4 want=$5 marker=$6
  rm -f "$tmp/posted"; printf '%s' "$comments" > "$tmp/comments.json"; mkdir -p "$tmp/rt"
  local o rc
  o=$(env PATH="$tmp:$PATH" FAKE_COMMENTS="$tmp/comments.json" POSTED="$tmp/posted" RUNNER_TEMP="$tmp/rt" GH_TOKEN=t INBOX=411 RESULTS=failure NOTE="runner x: 1 GB free" \
      DEDUPE_KEY="$key" DEDUPE_HOURS="$hrs" WORKFLOW="Runner disk alarm" RUN_URL=https://example/run SHA=abcdef1234567 REPO=purpusgit/.github bash "$tmp/post.sh" 2>&1); rc=$?
  local got=0; [ -s "$tmp/posted" ] && got=1
  local m=0; grep -qF "notify:$key" "$tmp/posted" 2>/dev/null && [ -n "$key" ] && m=1
  if [ "$want" = ERR ]; then [ "$rc" -ne 0 ] && ok "$name" || bad "$name: expected an error"; return; fi
  [ "$rc" -eq 0 ] && [ "$got" = "$want" ] && [ "$m" = "$marker" ] && ok "$name" || bad "$name: rc=$rc posted=$got marker=$m out=$o"
}
mk() { printf '[{"body":"**Runner disk alarm** ... runner x\\n\\n<!-- notify:%s -->","created_at":"%s"}]' "$1" "$2"; }
post "no dedupe_key: posts as before, no marker"          ""                 6 '[]'                                     1 0
post "key, nothing earlier: posts with the marker"        "runner-disk:x"    6 '[]'                                     1 1
post "same key 1 h ago: skipped"                          "runner-disk:x"    6 "$(mk runner-disk:x "$(now 1)")"         0 0
post "same key 7 h ago: posts again"                      "runner-disk:x"    6 "$(mk runner-disk:x "$(now 7)")"         1 1
post "another runner's marker 1 h ago: posts"             "runner-disk:y"    6 "$(mk runner-disk:x "$(now 1)")"         1 1
post "a comment that merely mentions the key, no marker"  "runner-disk:x"    6 '[{"body":"runner-disk:x is full","created_at":"'"$(now 1)"'"}]' 1 1
post "a 2 h window: same key 3 h ago posts"               "runner-disk:x"    2 "$(mk runner-disk:x "$(now 3)")"         1 1
post "a key with a space is refused"                      "runner disk"      6 '[]'                                     ERR 0
post "a key that could close the comment is refused"      "x--><b>"          6 '[]'                                     ERR 0
post "non-numeric hours are refused"                      "runner-disk:x"    abc '[]'                                   ERR 0
# an existing caller (no dedupe inputs) is unchanged: one POST, the old line format, and no GET of the inbox is needed
rm -f "$tmp/posted"; env PATH="$tmp:$PATH" FAKE_COMMENTS=/nonexistent POSTED="$tmp/posted" RUNNER_TEMP="$tmp/rt" GH_TOKEN=t INBOX=412 RESULTS="success skipped" NOTE="" DEDUPE_KEY="" DEDUPE_HOURS=6 \
  WORKFLOW="Web build" RUN_URL=https://example/run SHA=abcdef1234567 REPO=purpusgit/main_org_orbit bash "$tmp/post.sh" > /dev/null 2>&1
grep -q '"body": "\*\*Web build\*\* (purpusgit/main_org_orbit): \*\*success\*\*, head `abcdef1`, run https://example/run"' "$tmp/posted" && ok "an existing caller's comment is byte-identical in format" || bad "existing format changed: $(cat "$tmp/posted" 2>/dev/null)"
exit $fail
