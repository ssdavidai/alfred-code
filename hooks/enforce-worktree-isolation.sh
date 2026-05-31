#!/usr/bin/env bash
# enforce-worktree-isolation.sh — PreToolUse(Agent)
#
# Blocks `Agent` dispatches that look like lane work but don't set
# `isolation: "worktree"`. Tonight's lesson (2026-05-30): the #120 orchestrator
# and three #114 lane agents all shared /Users/ssd/dev/alfred. They reverted
# each other's edits on migrate.ts, deleted draft files, and caused two
# rebases. With `isolation: "worktree"` set, the harness gives each agent
# its own ephemeral git worktree.
#
# Detection rule: if the prompt mentions any of {lane, orchestrate, dispatch,
# fan out, fan-out, lane-out, parallel agents} AND `isolation` is not set
# to `worktree`, block.
#
# Allow override with `ALFRED_CODE_SKIP_WORKTREE_ENFORCEMENT=1` for
# investigation-only dispatches that don't write code.
#
# Exit 2 = hard block. Exit 0 = allow.
set -euo pipefail

if [[ "${ALFRED_CODE_SKIP_WORKTREE_ENFORCEMENT:-0}" == "1" ]]; then
  exit 0
fi

input=$(cat)
prompt=$(echo "$input" | jq -r '.tool_input.prompt // ""' 2>/dev/null || true)
isolation=$(echo "$input" | jq -r '.tool_input.isolation // ""' 2>/dev/null || true)

# Lane-shaped keywords (case-insensitive).
if echo "$prompt" | grep -qiE '\b(lane|orchestrate|dispatch|fan.?out|lane-out|parallel agents?)\b'; then
  if [[ "$isolation" != "worktree" ]]; then
    {
      echo "BLOCKED: lane-shaped Agent dispatch missing isolation: \"worktree\"."
      echo "Tonight's lesson (#120 Lane I/II/III collisions): multiple lane"
      echo "agents sharing one working tree revert each other's edits to"
      echo "migrate.ts and tests/migrate.test.ts."
      echo ""
      echo "Fix: add this to the Agent tool call's parameters:"
      echo "  \"isolation\": \"worktree\""
      echo ""
      echo "If this dispatch is read-only investigation (no Edit/Write),"
      echo "set ALFRED_CODE_SKIP_WORKTREE_ENFORCEMENT=1 in the env."
    } >&2
    exit 2
  fi
fi

exit 0
