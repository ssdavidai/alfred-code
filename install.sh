#!/usr/bin/env bash
# alfred-code drop-in installer.
# Symlinks hooks/ commands/ agents/ from this repo into your ~/.claude/,
# and merges settings.json.template into ~/.claude/settings.json.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${HOME}/.claude"
DRY_RUN="${DRY_RUN:-0}"

say() { printf "  %s\n" "$*"; }
do_or_say() {
  if [[ "$DRY_RUN" == "1" ]]; then
    say "[dry-run] $*"
  else
    eval "$@"
  fi
}

[[ -d "$TARGET" ]] || mkdir -p "$TARGET"

echo "═══════════════════════════════════════════════════════════════"
echo "  alfred-code install — Tier 1 (hooks + commands + agents)"
echo "═══════════════════════════════════════════════════════════════"
echo
echo "  source:  $SCRIPT_DIR"
echo "  target:  $TARGET"
echo

# ── 1. Symlink hooks ─────────────────────────────────────────────
echo "  → hooks/"
mkdir -p "$TARGET/hooks"
for h in "$SCRIPT_DIR/hooks"/*.sh; do
  name=$(basename "$h")
  if [[ -e "$TARGET/hooks/$name" && ! -L "$TARGET/hooks/$name" ]]; then
    say "    skip (exists, not a symlink): $name"
    continue
  fi
  do_or_say "ln -sf \"$h\" \"$TARGET/hooks/$name\""
  do_or_say "chmod +x \"$h\""
  say "    ✓ $name"
done

# ── 2. Symlink commands ──────────────────────────────────────────
echo
echo "  → commands/"
mkdir -p "$TARGET/commands"
for c in "$SCRIPT_DIR/commands"/*.md; do
  name=$(basename "$c")
  if [[ -e "$TARGET/commands/$name" && ! -L "$TARGET/commands/$name" ]]; then
    say "    skip (exists, not a symlink): $name"
    continue
  fi
  do_or_say "ln -sf \"$c\" \"$TARGET/commands/$name\""
  say "    ✓ /${name%.md}"
done

# ── 3. Symlink agents ────────────────────────────────────────────
echo
echo "  → agents/"
mkdir -p "$TARGET/agents"
for a in "$SCRIPT_DIR/agents"/*.md; do
  name=$(basename "$a")
  if [[ -e "$TARGET/agents/$name" && ! -L "$TARGET/agents/$name" ]]; then
    say "    skip (exists, not a symlink): $name"
    continue
  fi
  do_or_say "ln -sf \"$a\" \"$TARGET/agents/$name\""
  say "    ✓ ${name%.md}"
done

# ── 4. Merge settings.json.template ──────────────────────────────
echo
echo "  → settings.json"
if [[ -f "$TARGET/settings.json" ]]; then
  say "    existing settings.json found"
  say "    DRY merge preview (write to $TARGET/settings.json.alfred-code-preview):"
  if [[ "$DRY_RUN" != "1" ]]; then
    jq -s '.[0] * .[1]' "$TARGET/settings.json" "$SCRIPT_DIR/settings.json.template" \
      > "$TARGET/settings.json.alfred-code-preview"
    say "    preview written. Review then:"
    say "      mv $TARGET/settings.json.alfred-code-preview $TARGET/settings.json"
  fi
else
  do_or_say "cp \"$SCRIPT_DIR/settings.json.template\" \"$TARGET/settings.json\""
  say "    ✓ new settings.json installed"
fi

echo
echo "  Tier 1 install complete."
echo
echo "  Verify:"
echo "    claude --help-command lane-out"
echo "    ls -la $TARGET/{hooks,commands,agents}"
echo
echo "  Tier 2 (Telegram channel + GitHub webhooks):"
echo "    $SCRIPT_DIR/install-tier-2.sh"
echo
