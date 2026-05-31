#!/usr/bin/env bash
# token-budget-warn.sh — UserPromptSubmit
#
# Warns when the session's token spend crosses a threshold. Doesn't block;
# just surfaces the number so Sir doesn't accidentally rack up a large bill.
#
# Reads from `~/.claude/session-stats.json` if Claude Code writes it
# (introduced in a recent version). Falls back to a no-op if absent.
#
# Configurable via ALFRED_CODE_TOKEN_WARN (default 100000 tokens ≈ $1.50 @
# Sonnet rates, ≈ $7.50 @ Opus rates).
set -euo pipefail

STATS="${HOME}/.claude/session-stats.json"
WARN_AT="${ALFRED_CODE_TOKEN_WARN:-100000}"
CACHE="${HOME}/.cache/alfred-code/last-warn-token-count"

mkdir -p "$(dirname "$CACHE")"

[[ -f "$STATS" ]] || exit 0

# Try a few likely paths in the stats JSON.
tokens=$(jq -r '
  .current_session.total_tokens //
  .total_tokens //
  .session.tokens.total //
  0
' "$STATS" 2>/dev/null)

[[ -z "$tokens" || "$tokens" == "null" ]] && exit 0

# Don't double-warn — only fire when we cross a threshold for the first time.
last_warn=$(cat "$CACHE" 2>/dev/null || echo 0)

if [[ "$tokens" -ge "$WARN_AT" && "$last_warn" -lt "$WARN_AT" ]]; then
  echo "$tokens" > "$CACHE"
  echo "alfred-code: session at ${tokens} tokens (~\$$(echo "scale=2; $tokens/1000000*3" | bc) @ Sonnet, ~\$$(echo "scale=2; $tokens/1000000*15" | bc) @ Opus). Consider /clear if you're done with the current thread." >&2
fi

# Also fire every doubling after the initial warn (200k, 400k, 800k...)
for mult in 2 4 8 16; do
  threshold=$((WARN_AT * mult))
  if [[ "$tokens" -ge "$threshold" && "$last_warn" -lt "$threshold" ]]; then
    echo "$tokens" > "$CACHE"
    echo "alfred-code: session at ${tokens} tokens — ${mult}x the warn threshold. /clear or end the session if the work is done." >&2
    break
  fi
done

exit 0
