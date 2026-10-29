#!/usr/bin/env bash
# Pick the first working Claude credential (specs#721): ANTHROPIC_KEY_1..4 in expiry order,
# then slot 5, the OpenRouter fallback token. Never prints a key, a hash or a length.
# Slots 1-4 export ANTHROPIC_API_KEY. Slot 5 exports ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
# and an empty ANTHROPIC_API_KEY. Writes slot/exhausted to $GITHUB_OUTPUT.
set -uo pipefail

FALLBACK_BASE_URL=https://openrouter.ai/api

# $1 = header line, $2 = base URL, $3 = model; echoes the HTTP status, 000 when unreachable.
# The credential goes in via -H @file, never argv (/proc/*/cmdline).
probe() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$2/v1/messages" \
    -H @<(printf '%s\n' "$1") -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d "{\"model\":\"$3\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}"
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

try() { # $1 = header line, $2 = base URL, $3 = model; sets $status, returns 0 on ok
  local attempt verdict
  for attempt in 1 2; do
    status=$(probe "$1" "$2" "$3")
    verdict=$(classify "$status")
    [ "$verdict" = retry ] && [ "$attempt" = 1 ] && { sleep "${ANTHROPIC_KEY_RETRY_SECONDS:-5}"; continue; }
    break
  done
  [ "$verdict" = ok ]
}

log=""
for n in 1 2 3 4; do
  var="ANTHROPIC_KEY_$n"; key="${!var:-}"
  if [ -z "$key" ]; then log="$log slot $n: unset;"; continue; fi
  echo "::add-mask::$key"
  if try "x-api-key: $key" https://api.anthropic.com claude-haiku-5-5; then
    echo "ANTHROPIC_API_KEY=$key" >> "$GITHUB_ENV"
    { echo "slot=$n"; echo "exhausted=false"; } >> "$GITHUB_OUTPUT"
    echo "anthropic-key: slot $n in use (${log# })"
    exit 0
  fi
  log="$log slot $n: $(reason "$status");"
done

token="${ANTHROPIC_FALLBACK_TOKEN:-}"
if [ -z "$token" ]; then
  log="$log slot 5: unset;"
else
  echo "::add-mask::$token"
  if try "Authorization: Bearer $token" "$FALLBACK_BASE_URL" anthropic/claude-haiku-5.5; then
    { echo "ANTHROPIC_BASE_URL=$FALLBACK_BASE_URL"; echo "ANTHROPIC_AUTH_TOKEN=$token"; echo "ANTHROPIC_API_KEY="; } >> "$GITHUB_ENV"
    { echo "slot=5"; echo "exhausted=false"; } >> "$GITHUB_OUTPUT"
    echo "anthropic-key: slot 5 (OpenRouter fallback) in use (${log# })"
    exit 0
  fi
  log="$log slot 5: $(reason "$status");"
fi

{ echo "slot="; echo "exhausted=true"; } >> "$GITHUB_OUTPUT"
echo "::notice title=Claude credit::Claude credit exhausted on all five slots (${log# }). Skipping the Claude step."
exit 0
