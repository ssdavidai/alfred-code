#!/usr/bin/env bash
# install-tier-4.sh — wire up the secrets broker.
#
# Run AFTER install.sh (Tier 1). Tier 2 + 3 are optional dependencies.
#
# What this does:
#   1. Symlinks bin/secret + bin/secret-set into ~/.claude/bin/
#   2. Adds ~/.claude/bin/ to PATH via a shell-profile snippet (if needed)
#   3. Symlinks hooks/inject-secrets-registry.sh into ~/.claude/hooks/
#   4. Symlinks commands/secrets-bootstrap.md + secrets-status.md
#   5. Registers the SessionStart hook in ~/.claude/settings.json
#   6. Prints which canonical secrets are still missing
set -euo pipefail

ALFRED_CODE_HOME="${ALFRED_CODE_HOME:-$HOME/.claude/alfred-code}"
TARGET="${HOME}/.claude"

say()   { printf "  %s\n" "$*"; }
green() { printf "\033[32m%s\033[0m" "$*"; }
red()   { printf "\033[31m%s\033[0m" "$*"; }

# ── sanity ──────────────────────────────────────────────────────
echo
echo "  ╔══════════════════════════════════════════════════════════════╗"
echo "  ║  alfred-code — Tier 4 install (secrets broker)              ║"
echo "  ╚══════════════════════════════════════════════════════════════╝"
echo
if [[ ! -d "$TARGET/hooks" ]]; then
  say "$(red ERROR): Tier 1 not installed. Run install.sh first."
  exit 1
fi
say "$(green ✓) Tier 1 detected."

# ── 1. Symlink bin/ ─────────────────────────────────────────────
echo
echo "  → bin/"
mkdir -p "$TARGET/bin"
for b in secret secret-set alfred-code-dispatch alfred-code-reap-worktrees alfred-code-fix-pr alfred-code-poll-lock; do
  src="$ALFRED_CODE_HOME/bin/$b"
  dst="$TARGET/bin/$b"
  if [[ ! -f "$src" ]]; then
    say "$(red ✗) source missing: $src"
    exit 1
  fi
  chmod +x "$src"
  if [[ -e "$dst" && ! -L "$dst" ]]; then
    say "    skip (exists, not a symlink): $b"
  else
    ln -sf "$src" "$dst"
    say "    $(green ✓) $b"
  fi
done

# ── 2. PATH advice ──────────────────────────────────────────────
echo
echo "  → PATH"
if echo ":$PATH:" | grep -q ":$TARGET/bin:"; then
  say "    $(green ✓) $TARGET/bin already on PATH"
else
  shell_rc=""
  case "${SHELL:-}" in
    */zsh)  shell_rc="$HOME/.zshrc" ;;
    */bash) shell_rc="$HOME/.bashrc" ;;
  esac
  if [[ -n "$shell_rc" && -f "$shell_rc" ]]; then
    if ! grep -q "alfred-code/bin" "$shell_rc" 2>/dev/null; then
      cat >> "$shell_rc" <<'PATHADD'

# alfred-code secrets broker
export PATH="$HOME/.claude/bin:$PATH"
PATHADD
      say "    $(green ✓) appended PATH export to $shell_rc"
      say "    Open a new shell or run: source $shell_rc"
    else
      say "    $(green ✓) $shell_rc already has the export"
    fi
  else
    say "    add to your shell profile manually:"
    say "      export PATH=\"\$HOME/.claude/bin:\$PATH\""
  fi
fi

# ── 3. Symlink SessionStart hook ────────────────────────────────
echo
echo "  → hooks/"
hook_src="$ALFRED_CODE_HOME/hooks/inject-secrets-registry.sh"
hook_dst="$TARGET/hooks/inject-secrets-registry.sh"
chmod +x "$hook_src"
if [[ -e "$hook_dst" && ! -L "$hook_dst" ]]; then
  say "    skip (exists, not a symlink)"
else
  ln -sf "$hook_src" "$hook_dst"
  say "    $(green ✓) inject-secrets-registry.sh"
fi

# ── 4. Symlink commands ─────────────────────────────────────────
echo
echo "  → commands/"
for c in secrets-bootstrap.md secrets-status.md; do
  src="$ALFRED_CODE_HOME/commands/$c"
  dst="$TARGET/commands/$c"
  if [[ -e "$dst" && ! -L "$dst" ]]; then
    say "    skip (exists, not a symlink): $c"
  else
    ln -sf "$src" "$dst"
    say "    $(green ✓) /${c%.md}"
  fi
done

# ── 5. Register hook in settings.json ───────────────────────────
echo
echo "  → settings.json"
SETTINGS="$TARGET/settings.json"
if [[ ! -f "$SETTINGS" ]]; then
  say "    $(red ✗) settings.json missing — run install.sh first"
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  say "    $(red ✗) jq missing — install with: brew install jq"
  exit 1
fi
tmp=$(mktemp)
jq '
  .hooks.SessionStart = ((.hooks.SessionStart // []) + [
    {command: "$CLAUDE_HOME/hooks/inject-secrets-registry.sh"}
  ] | unique_by(.command))
' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
say "    $(green ✓) SessionStart hook registered"

# ── 6. Status report ────────────────────────────────────────────
echo
echo "  → status"
set +e
"$ALFRED_CODE_HOME/bin/secret" names | while IFS= read -r name; do
  if "$ALFRED_CODE_HOME/bin/secret" has "$name" >/dev/null 2>&1; then
    printf "    $(green ✓) %s\n" "$name"
  else
    printf "    %s missing\n" "$name"
  fi
done
set -e

echo
echo "  ╔══════════════════════════════════════════════════════════════╗"
echo "  ║              $(green Tier 4 installed.)                          ║"
echo "  ╚══════════════════════════════════════════════════════════════╝"
echo
echo "  Next:"
echo "    1. Open a new shell so PATH picks up ~/.claude/bin"
echo "    2. Run /secrets-bootstrap in a Claude session to fill in"
echo "       the missing ones. Each prompt is silent — values never"
echo "       hit your terminal scrollback."
echo "    3. From now on, Claude uses 'secret get <name>' inline and"
echo "       never asks you for tokens again."
echo
echo "  Verify standalone:"
echo "      secret list       # set names only, no values"
echo "      secret names      # all canonical names"
echo "      secret where cloudflare-api-token"
echo
echo "  Docs: ~/.claude/alfred-code/docs/secrets-broker.md"
echo
