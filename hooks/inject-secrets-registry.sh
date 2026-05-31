#!/usr/bin/env bash
# SessionStart hook — tells Claude which secrets are available without
# revealing values. Output goes into the session as a system-reminder.
#
# Wired in settings.json:
#   "hooks": {
#     "SessionStart": [
#       { "command": "$CLAUDE_HOME/hooks/inject-secrets-registry.sh" }
#     ]
#   }
#
# Output protocol: this hook prints JSON to stdout matching Claude Code's
# hook-output schema, which the runtime injects as additional context.

set -euo pipefail

ALFRED_SECRETS_HOME="${ALFRED_SECRETS_HOME:-$HOME/.claude/alfred-code}"
SECRET_BIN="${ALFRED_SECRETS_HOME}/bin/secret"

# If the broker isn't installed, no-op (don't error — Tier 1 users might
# not have Tier 4 wired yet).
if [[ ! -x "$SECRET_BIN" ]]; then
  exit 0
fi

# Collect available names (the broker `list` subcommand only emits names,
# never values).
available=$("$SECRET_BIN" list 2>/dev/null || true)
missing=$("$SECRET_BIN" names 2>/dev/null | while IFS= read -r name; do
  if ! "$SECRET_BIN" has "$name" 2>/dev/null; then
    printf '%s\n' "$name"
  fi
done)

# Build the reminder. Use jq for safe JSON encoding.
if command -v jq >/dev/null 2>&1; then
  reminder=$(jq -nRr --arg avail "$available" --arg missing "$missing" '
    "Secrets broker is wired. Use `secret get <name>` to fetch inline (NEVER echo values to conversation; use $(secret get …) in curl/etc).\n\nAvailable (set in Keychain):\n" +
    ($avail | split("\n") | map(select(length > 0)) | map("  • " + .) | join("\n")) +
    (if ($missing | length > 0) then
      "\n\nMissing (not yet set; ask user before running `/secrets-bootstrap`):\n" +
      ($missing | split("\n") | map(select(length > 0)) | map("  • " + .) | join("\n"))
     else "" end) +
    "\n\nUsage pattern:\n  curl -H \"Authorization: Bearer $(secret get cloudflare-api-token)\" …\n  export HCLOUD_TOKEN=$(secret get hetzner-api-token); hcloud …\n\nNever: `env`, `printenv` (any name), or echoing $(secret get …) to stdout."
  ')

  jq -nc --arg msg "$reminder" '{
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: $msg
    }
  }'
else
  # Fallback: stderr advisory, no JSON injection.
  {
    echo "alfred-code secrets broker is wired but jq is missing —"
    echo "  install jq so the SessionStart reminder can be injected:"
    echo "    brew install jq"
  } >&2
fi
