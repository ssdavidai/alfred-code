#!/usr/bin/env bash
# install-tier-3.sh — polish: kill criteria + token budget + ADR + PM dashboard.
#
# Run AFTER install.sh + install-tier-2.sh. Idempotent.
set -euo pipefail

ALFRED_CODE_HOME="${ALFRED_CODE_HOME:-$HOME/.claude/alfred-code}"

say()   { printf "  %s\n" "$*"; }
green() { printf "\033[32m%s\033[0m" "$*"; }

# ─── 1. Link Tier 3 hooks ───────────────────────────────────────
echo
echo "  → Tier 3 hooks"
for h in kill-criteria.sh token-budget-warn.sh; do
  if [[ -e "$HOME/.claude/hooks/$h" && ! -L "$HOME/.claude/hooks/$h" ]]; then
    say "    skip (exists, not a symlink): $h"
  else
    ln -sf "$ALFRED_CODE_HOME/hooks/$h" "$HOME/.claude/hooks/$h"
    chmod +x "$ALFRED_CODE_HOME/hooks/$h"
    say "    $(green ✓) $h"
  fi
done

# ─── 2. Register them in settings.json ──────────────────────────
echo
echo "  → Updating settings.json with Tier 3 hooks"
SETTINGS="$HOME/.claude/settings.json"
if [[ ! -f "$SETTINGS" ]]; then
  say "    settings.json missing; run install.sh first."
  exit 1
fi

# Append the new hooks (deduped) to the relevant arrays.
tmp=$(mktemp)
jq '
  .hooks.PreToolUse += [
    {matcher: "Agent", command: "$CLAUDE_HOME/hooks/kill-criteria.sh"}
  ] |
  .hooks.UserPromptSubmit += [
    {command: "$CLAUDE_HOME/hooks/token-budget-warn.sh"}
  ] |
  # Dedup: keep entries unique by command
  .hooks.PreToolUse |= unique_by(.command) |
  .hooks.UserPromptSubmit |= unique_by(.command)
' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
say "    $(green ✓) settings.json updated"

# ─── 3. Link Tier 3 commands ────────────────────────────────────
echo
echo "  → Tier 3 commands"
for c in file-adr.md pm-dashboard.md; do
  if [[ -e "$HOME/.claude/commands/$c" && ! -L "$HOME/.claude/commands/$c" ]]; then
    say "    skip (exists, not a symlink): $c"
  else
    ln -sf "$ALFRED_CODE_HOME/commands/$c" "$HOME/.claude/commands/$c"
    say "    $(green ✓) /${c%.md}"
  fi
done

# ─── 4. ADR scaffolding in the working repo ─────────────────────
echo
echo "  → ADR scaffolding"
WORKING_REPO="${WORKING_REPO:-/Users/ssd/dev/alfred}"
if [[ -d "$WORKING_REPO/.git" ]]; then
  mkdir -p "$WORKING_REPO/docs/decisions"
  if [[ ! -f "$WORKING_REPO/docs/decisions/ADR-template.md" ]]; then
    cp "$ALFRED_CODE_HOME/docs/decisions/ADR-template.md" "$WORKING_REPO/docs/decisions/"
    cp "$ALFRED_CODE_HOME/docs/decisions/README.md" "$WORKING_REPO/docs/decisions/"
    say "    $(green ✓) Scaffolded $WORKING_REPO/docs/decisions/"
    say "    Don't forget to: git add docs/decisions && git commit && git push"
  else
    say "    skip — ADR template already present in $WORKING_REPO"
  fi
else
  say "    no working repo found at $WORKING_REPO (set WORKING_REPO= to override)"
fi

cat <<EOF

  $(green Tier 3 installed.)

  New behaviours:
    - After 3 partial-iteration dispatches on the same issue,
      future dispatches block until you clear ~/.alfred-code-state/stuck.json
      (raise the threshold with ALFRED_CODE_MAX_PARTIALS=5)

    - Token budget warning fires at 100k tokens per session
      (raise with ALFRED_CODE_TOKEN_WARN=200000)

    - /file-adr drafts an Architecture Decision Record
    - /pm-dashboard emits a markdown PM dashboard

  Optional Tier 3 polish you can add manually:
    - Daily digest scheduled task running /pm-dashboard at 08:00
    - Per-repo CLAUDE.md additions for repo-specific gotchas

EOF
