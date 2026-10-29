#!/usr/bin/env bash
# Self-test for probe.sh with a fake curl on PATH. Run: bash probe.test.sh
set -u
here=$(cd "$(dirname "$0")" && pwd); tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# Fake curl: reads the x-api-key or Bearer header from the -H @file arg; status = $tmp/status.<key> (one line per call).
cat > "$tmp/curl" <<'F'
#!/usr/bin/env bash
for a in "$@"; do case "$a" in @*) key=$(sed -n -e 's/^x-api-key: //p' -e 's/^Authorization: Bearer //p' "${a#@}");; esac; done
f="$FAKE_DIR/status.$key"; s=$(head -1 "$f"); sed -i 1d "$f"; [ -s "$f" ] || echo "$s" > "$f"; printf '%s' "$s"
F
chmod +x "$tmp/curl"; fail=0
run() { # name, expected slot, expected exhausted, then key=status-seq pairs
  local name=$1 want_slot=$2 want_ex=$3; shift 3
  rm -f "$tmp"/status.* "$tmp/env" "$tmp/out"; local i=0; local -a envs=()
  for kv in "$@"; do i=$((i+1)); k=${kv%%=*}; v=${kv#*=}
    ev="ANTHROPIC_KEY_$i"; [ "$i" = 5 ] && ev=ANTHROPIC_FALLBACK_TOKEN
    [ -n "$k" ] && { tr ',' '\n' <<<"$v" > "$tmp/status.$k"; envs+=("$ev=$k"); }; done
  log=$(env -i PATH="$tmp:$PATH" FAKE_DIR="$tmp" GITHUB_ENV="$tmp/env" GITHUB_OUTPUT="$tmp/out" \
        ANTHROPIC_KEY_RETRY_SECONDS=0 "${envs[@]}" bash "$here/probe.sh")
  slot=$(sed -n 's/^slot=//p' "$tmp/out"); ex=$(sed -n 's/^exhausted=//p' "$tmp/out")
  if [ "$slot" = "$want_slot" ] && [ "$ex" = "$want_ex" ] && ! grep -q 'sk-' <<<"$(grep -v '::add-mask::' <<<"$log")"; then
    echo "ok   $name"; else echo "FAIL $name: slot=$slot exhausted=$ex log=$log"; fail=1; fi
  if [ "$want_ex" = false ] && [ "$want_slot" != 5 ] && ! grep -qx "ANTHROPIC_API_KEY=sk-$want_slot" "$tmp/env"; then echo "FAIL $name: env"; fail=1; fi
  if [ "$want_slot" = 5 ] && ! { grep -qx "ANTHROPIC_AUTH_TOKEN=sk-5" "$tmp/env" && grep -qx "ANTHROPIC_BASE_URL=https://openrouter.ai/api" "$tmp/env" && grep -qx "ANTHROPIC_API_KEY=" "$tmp/env"; }; then echo "FAIL $name: slot-5 env"; fail=1; fi
}
run "first key works"            1 false sk-1=200 sk-2=200 sk-3=200 sk-4=200
run "credit 400 -> next"         2 false sk-1=400 sk-2=200 sk-3=200 sk-4=200
run "revoked 401 -> next"        3 false sk-1=401 sk-2=403 sk-3=200 sk-4=200
run "one 429 then ok stays"      1 false sk-1=429,200 sk-2=200 sk-3=200 sk-4=200
run "two 429 -> next"            2 false sk-1=429,429 sk-2=200 sk-3=200 sk-4=200
run "5xx twice -> next"          4 false sk-1=500,503 sk-2=401 sk-3=400 sk-4=200
run "unset slot skipped"         3 false =x =x sk-3=200 sk-4=200
run "1-4 out, no fallback"       "" true sk-1=400 sk-2=400 sk-3=401 sk-4=429,429
run "1-4 out -> slot 5"          5 false sk-1=400 sk-2=400 sk-3=401 sk-4=429,429 sk-5=200
run "slot 5 not used if 1 ok"    1 false sk-1=200 sk-2=200 sk-3=200 sk-4=200 sk-5=200
run "all five out"               "" true sk-1=400 sk-2=400 sk-3=401 sk-4=400 sk-5=402
exit $fail
