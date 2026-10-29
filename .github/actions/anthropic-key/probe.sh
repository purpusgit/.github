#!/usr/bin/env bash
# Pick the first working Claude key from ANTHROPIC_KEY_1..4 (expiry order, specs#721).
# Never prints a key, a hash or a length. Writes slot/exhausted to $GITHUB_OUTPUT and
# the chosen key to $GITHUB_ENV as ANTHROPIC_API_KEY.
set -uo pipefail

probe() { # $1 = key; echoes the HTTP status, 000 when unreachable. The key goes in via -H @file, never argv (/proc/*/cmdline).
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    https://api.anthropic.com/v1/messages \
    -H @<(printf 'x-api-key: %s\n' "$1") -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d '{"model":"claude-haiku-5-5","max_tokens":1,"messages":[{"role":"user","content":"ok"}]}'
}

classify() { # $1 = status; echoes ok | next | retry
  case "$1" in
    200) echo ok ;;
    401|403) echo next ;;
    400) echo next ;;  # credit exhausted or bad request: either way this key cannot serve the job
    429|5??|000) echo retry ;;
    *) echo next ;;
  esac
}

reason() { # short log word for a status
  case "$1" in 401|403) echo invalid ;; 400) echo credit-or-400 ;; 429) echo rate-limited ;; 000) echo unreachable ;; *) echo "http-$1" ;; esac
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
