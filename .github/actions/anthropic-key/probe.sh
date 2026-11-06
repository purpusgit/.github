#!/usr/bin/env bash
# Pick the first working Claude key from ANTHROPIC_KEY_1..4 (expiry order, specs#721).
# Never prints a key, a hash or a length. Writes slot/exhausted to $GITHUB_OUTPUT and
# the chosen key to $GITHUB_ENV as ANTHROPIC_API_KEY.
set -uo pipefail

BODY=$(mktemp); trap 'rm -f "$BODY"' EXIT

probe() { # $1 = key; echoes the HTTP status (000 when unreachable), the error body in $BODY. The key goes in via -H @file, never argv (/proc/*/cmdline).
  curl -s -o "$BODY" -w '%{http_code}' --max-time 20 \
    https://api.anthropic.com/v1/messages \
    -H @<(printf 'x-api-key: %s\n' "$1") -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d '{"model":"claude-haiku-5-5","max_tokens":1,"messages":[{"role":"user","content":"ok"}]}'
}

# Same rules as the agent runner (orbit-deploy tools/agent-runner/run.py): a 400 that doesn't name credit or
# billing, or a 404/413/422, is the PROBE being wrong (a retired model id), not this key, so no slot is judged.
classify() { # $1 = status; echoes ok | next | retry | rejected
  case "$1" in
    200) echo ok ;;
    401|403|402|3??) echo next ;;
    400) if grep -qiE 'credit|billing' "$BODY"; then echo next; else echo rejected; fi ;;
    404|413|422) echo rejected ;;
    429|5??|000) echo retry ;;
    *) echo next ;;
  esac
}

reason() { # short log word for a status
  case "$1" in 401|403) echo invalid ;; 400|402) echo credit ;; 3??) echo redirected ;; 429) echo rate-limited ;; 000) echo unreachable ;; *) echo "http-$1" ;; esac
}

log=""
for n in 1 2 3 4; do
  var="ANTHROPIC_KEY_$n"; key="${!var:-}"
  if [ -z "$key" ]; then log="$log slot $n: unset;"; continue; fi
  echo "::add-mask::$key"
  for attempt in 1 2; do
    status=$(probe "$key")
    verdict=$(classify "$status")
    [ "$verdict" = retry ] && [ "$attempt" = 1 ] && { sleep "${ANTHROPIC_KEY_RETRY_SECONDS:-5}"; continue; }
    break
  done
  if [ "$verdict" = rejected ]; then
    { echo "slot="; echo "exhausted=true"; } >> "$GITHUB_OUTPUT"
    echo "::warning title=Claude probe rejected::The API rejected the probe itself (http $status, likely a retired probe model id in actions/anthropic-key/probe.sh); no key was judged. Skipping the Claude step."
    exit 0
  fi
  if [ "$verdict" = ok ]; then
    echo "ANTHROPIC_API_KEY=$key" >> "$GITHUB_ENV"
    { echo "slot=$n"; echo "exhausted=false"; } >> "$GITHUB_OUTPUT"
    echo "anthropic-key: slot $n in use (${log# })"
    exit 0
  fi
  log="$log slot $n: $(reason "$status");"
done

{ echo "slot="; echo "exhausted=true"; } >> "$GITHUB_OUTPUT"
echo "::notice title=Claude credit::Claude credit exhausted on all four keys (${log# }). Skipping the Claude step."
exit 0
