#!/usr/bin/env bash
# kill-criteria.sh — PreToolUse(Agent)
#
# Stops the loop when an issue gets stuck. After N failed iterations on the
# same issue (where "iteration" = a lane dispatch that came back partial),
# block further dispatches until Sir intervenes.
#
# Policy: 3 partials per issue → block. Sir can override with /lane-out
# --force or by clearing the counter from `~/.alfred-code-state/stuck.json`.
#
# This is the "kill criteria" lesson from the multi-agent orchestration body
# of practice: if an agent's stuck after N tries, the problem usually isn't
# code, it's the spec.
set -euo pipefail

STUCK="${HOME}/.alfred-code-state/stuck.json"
MAX_PARTIALS="${ALFRED_CODE_MAX_PARTIALS:-3}"

mkdir -p "$(dirname "$STUCK")"
[[ -f "$STUCK" ]] || echo '{}' > "$STUCK"

input=$(cat)
prompt=$(echo "$input" | jq -r '.tool_input.prompt // ""' 2>/dev/null || true)

# Extract issue number from the prompt — looks for "#NNN" or "issue NNN".
issue=$(echo "$prompt" | grep -oE '(#|issue[[:space:]]+#?)[0-9]+' | grep -oE '[0-9]+' | head -1)

if [[ -z "$issue" ]]; then
  exit 0  # not an issue-shaped dispatch; let it through
fi

count=$(jq -r --arg i "$issue" '.[$i] // 0' "$STUCK" 2>/dev/null || echo 0)

if [[ "$count" -ge "$MAX_PARTIALS" ]]; then
  {
    echo "BLOCKED: issue #$issue has had $count partial dispatches (limit: $MAX_PARTIALS)."
    echo ""
    echo "The kill-criteria policy stops the loop when a lane keeps failing —"
    echo "usually the spec is the problem, not the code."
    echo ""
    echo "Next steps:"
    echo "  1. Read what went wrong: jq '.[\"$issue\"]' $STUCK"
    echo "  2. Read the most recent partial: gh issue view $issue --comments"
    echo "  3. Either re-spec the issue (close + reopen with corrections),"
    echo "     OR override the gate:"
    echo "       jq 'del(.[\"$issue\"])' $STUCK > /tmp/stuck.tmp && mv /tmp/stuck.tmp $STUCK"
    echo "     OR raise the threshold via ALFRED_CODE_MAX_PARTIALS=5"
  } >&2
  exit 2
fi

exit 0
