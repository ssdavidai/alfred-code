#!/usr/bin/env bash
# install-tier-2.sh — wires Telegram + GH Actions + Desktop scheduled task.
#
# Run this AFTER install.sh. It assumes Tier 1 is in place.
#
# What it does:
#   1. Walks Sir through BotFather token + chat_id capture
#   2. Writes ~/.alfred-code-state/.env
#   3. Installs telegram channel plugin via npx (or tells Sir to do it in
#      his Claude Code session)
#   4. Copies workflows/*.yml to a target repo's .github/workflows/
#   5. Sets GH secrets if `gh` is authenticated
#   6. Installs the alfred-code-poll Desktop scheduled task SKILL.md
#   7. Prints next-step instructions Sir has to do in the Desktop UI
set -euo pipefail

ALFRED_CODE_HOME="${ALFRED_CODE_HOME:-$HOME/.claude/alfred-code}"
STATE_DIR="${HOME}/.alfred-code-state"

say()  { printf "\n  %s\n" "$*"; }
ask()  { printf "  %s " "$*"; read -r REPLY; echo "$REPLY"; }
green(){ printf "\033[32m%s\033[0m" "$*"; }
red()  { printf "\033[31m%s\033[0m" "$*"; }

# ─── 1. Sanity: Tier 1 installed? ───────────────────────────────
if [[ ! -d "$HOME/.claude/hooks" ]] || [[ ! -L "$HOME/.claude/hooks/block-env-dump.sh" ]]; then
  say "$(red ERROR): Tier 1 not installed. Run ${ALFRED_CODE_HOME}/install.sh first."
  exit 1
fi
say "$(green ✓) Tier 1 installed."

# ─── 2. Sanity: Bun? gh? jq? ────────────────────────────────────
for cmd in bun gh jq curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    say "$(red ERROR): $cmd not installed. Install it and re-run."
    case "$cmd" in
      bun) say "  → curl -fsSL https://bun.sh/install | bash" ;;
      gh)  say "  → brew install gh && gh auth login" ;;
      jq)  say "  → brew install jq" ;;
    esac
    exit 1
  fi
done
say "$(green ✓) bun, gh, jq, curl all present."

# ─── 3. State directory ─────────────────────────────────────────
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
[[ -f "$STATE_DIR/last-tg-update-id" ]]   || echo 0 > "$STATE_DIR/last-tg-update-id"
[[ -f "$STATE_DIR/last-issue-poll" ]]     || date -u +%FT%TZ > "$STATE_DIR/last-issue-poll"
[[ -f "$STATE_DIR/pending-gates.json" ]]  || echo '{}' > "$STATE_DIR/pending-gates.json"
[[ -f "$STATE_DIR/dispatched.json" ]]     || echo '{}' > "$STATE_DIR/dispatched.json"
say "$(green ✓) State directory at $STATE_DIR"

# ─── 4. Bot token + chat_id ─────────────────────────────────────
if [[ -f "$STATE_DIR/.env" ]]; then
  say "$(green ✓) $STATE_DIR/.env exists; reusing existing config."
else
  cat <<'EOF'

  ╔══════════════════════════════════════════════════════════════╗
  ║  STEP 1 of 5 — Create the Telegram bot                       ║
  ╚══════════════════════════════════════════════════════════════╝

  1. Open Telegram on your phone.
  2. Search for @BotFather, tap to open a chat.
  3. Send: /newbot
  4. Set the display name to: Alfred Code
  5. Set a unique username ending in 'bot' (e.g. your_alfred_code_bot)
  6. BotFather replies with a token like 1234567890:AAEx_...

EOF
  TOKEN=$(ask "Paste the bot token (the whole thing):")
  [[ -z "$TOKEN" ]] && { say "$(red No token. Aborting.)"; exit 1; }

  cat <<'EOF'

  ╔══════════════════════════════════════════════════════════════╗
  ║  STEP 2 of 5 — Find your chat_id                             ║
  ╚══════════════════════════════════════════════════════════════╝

  1. In Telegram, find your new bot and send: hi
  2. (The bot won't reply yet — we'll pair it later.)
  3. We'll fetch your chat_id automatically.

EOF
  ask "Press ENTER once you've sent 'hi' to the bot:" > /dev/null

  CHAT_ID=$(curl -sS "https://api.telegram.org/bot${TOKEN}/getUpdates" | jq -r '.result[0].message.chat.id // empty')
  if [[ -z "$CHAT_ID" ]]; then
    say "$(red No chat found.) Send the word hi to the bot in Telegram, then re-run this script."
    exit 1
  fi
  say "$(green ✓) chat_id = $CHAT_ID"

  REPO=$(ask "Target repo for GH Actions (default: ssdavidai/alfred):")
  REPO="${REPO:-ssdavidai/alfred}"

  cat > "$STATE_DIR/.env" <<EOF
ALFRED_CODE_BOT_TOKEN=$TOKEN
ALFRED_CODE_CHAT_ID=$CHAT_ID
ALFRED_CODE_REPO=$REPO
EOF
  chmod 600 "$STATE_DIR/.env"
  say "$(green ✓) Wrote $STATE_DIR/.env"
fi

# Source the env
source "$STATE_DIR/.env"

# ─── 5. Outbound test ───────────────────────────────────────────
say "Sending test message to Telegram…"
response=$(curl -sS -X POST "https://api.telegram.org/bot${ALFRED_CODE_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${ALFRED_CODE_CHAT_ID}" \
  --data-urlencode "text=✅ alfred-code install-tier-2 outbound test")
if [[ "$(echo "$response" | jq -r '.ok')" == "true" ]]; then
  say "$(green ✓) Outbound to Telegram works. Check your phone."
else
  say "$(red ✗) Outbound failed: $response"
  exit 1
fi

# ─── 6. GitHub Actions ──────────────────────────────────────────
cat <<EOF

  ╔══════════════════════════════════════════════════════════════╗
  ║  STEP 3 of 5 — Install GH Actions in your target repo        ║
  ╚══════════════════════════════════════════════════════════════╝

EOF
REPO_PATH=$(ask "Local path to the repo (e.g. /Users/ssd/dev/alfred):")
if [[ ! -d "$REPO_PATH/.git" ]]; then
  say "$(red Not a git repo: $REPO_PATH. Skipping workflow install — you can do it manually.)"
else
  mkdir -p "$REPO_PATH/.github/workflows"
  for wf in notify-telegram.yml pr-review-gate.yml deploy-fleet.yml; do
    cp "$ALFRED_CODE_HOME/workflows/$wf" "$REPO_PATH/.github/workflows/$wf"
    say "$(green ✓) Copied $wf"
  done
  say "  Don't forget to: git add .github/workflows/*.yml && git commit && git push"

  # Set secrets
  if gh auth status >/dev/null 2>&1; then
    say "Setting GitHub secrets in ${ALFRED_CODE_REPO}…"
    echo -n "$ALFRED_CODE_BOT_TOKEN" | gh secret set ALFRED_CODE_BOT_TOKEN --repo "$ALFRED_CODE_REPO"
    echo -n "$ALFRED_CODE_CHAT_ID"   | gh secret set ALFRED_CODE_CHAT_ID --repo "$ALFRED_CODE_REPO"
    if [[ -f "$HOME/.ssh/alfred-black-verify" ]]; then
      gh secret set FLEET_SSH_KEY --repo "$ALFRED_CODE_REPO" < "$HOME/.ssh/alfred-black-verify"
      say "$(green ✓) Set FLEET_SSH_KEY"
    else
      say "  → Set FLEET_SSH_KEY manually: gh secret set FLEET_SSH_KEY < <path-to-private-key>"
    fi
    say "$(green ✓) Set ALFRED_CODE_BOT_TOKEN + ALFRED_CODE_CHAT_ID"
  else
    say "$(red ✗) gh not authenticated. Run 'gh auth login' then set secrets manually:"
    say "    gh secret set ALFRED_CODE_BOT_TOKEN"
    say "    gh secret set ALFRED_CODE_CHAT_ID"
    say "    gh secret set FLEET_SSH_KEY < ~/.ssh/alfred-black-verify"
  fi
fi

# ─── 7. Scheduled task SKILL.md ─────────────────────────────────
cat <<EOF

  ╔══════════════════════════════════════════════════════════════╗
  ║  STEP 4 of 5 — Install the Desktop scheduled task            ║
  ╚══════════════════════════════════════════════════════════════╝

EOF
TASK_DIR="$HOME/.claude/scheduled-tasks/alfred-code-poll"
mkdir -p "$TASK_DIR"
cp "$ALFRED_CODE_HOME/cron/alfred-code-poll.SKILL.md" "$TASK_DIR/SKILL.md"
say "$(green ✓) Installed SKILL.md at $TASK_DIR"

cat <<EOF

  Now configure the schedule in the Desktop UI:

    1. Open Claude Code Desktop
    2. Click Routines in the sidebar
    3. The task 'alfred-code-poll' should appear
    4. Set:
       - Working folder: $REPO_PATH
       - Worktree toggle: ON
       - Permission mode: Auto-approve
       - Schedule: ask Claude in any Desktop session
         "set alfred-code-poll to run every 5 minutes"
    5. Click 'Run now' to test
    6. Approve permission prompts; pick 'Always allow'

EOF

# ─── 8. Pair Telegram channel ──────────────────────────────────
cat <<EOF

  ╔══════════════════════════════════════════════════════════════╗
  ║  STEP 5 of 5 — Install Telegram channel plugin in Claude     ║
  ╚══════════════════════════════════════════════════════════════╝

  In a Claude Code session (Desktop OR CLI), run:

      /plugin marketplace add anthropics/claude-plugins-official
      /plugin install telegram@claude-plugins-official
      /reload-plugins
      /telegram:configure $ALFRED_CODE_BOT_TOKEN

  Then quit Claude and re-launch with channels enabled:

      claude --channels plugin:telegram@claude-plugins-official

  Add this alias to ~/.zshrc:

      alias claude-tg='claude --channels plugin:telegram@claude-plugins-official'

  Pair the bot:

      # In Telegram, send any message to the bot
      # The bot replies with a pairing code
      /telegram:access pair <code>
      /telegram:access policy allowlist

EOF

echo
echo "  ╔══════════════════════════════════════════════════════════════╗"
echo "  ║                  $(green 'Tier 2 installed.')                       ║"
echo "  ╚══════════════════════════════════════════════════════════════╝"
echo
echo "  Test end-to-end:"
echo
echo "      gh issue create --title 'Test alfred-code' --body 'skip me'"
echo
echo "  Within ~10 seconds: Telegram pings you with the new issue."
echo "  Within ~5 minutes:  Claude wakes via the scheduled task, triages,"
echo "                      posts a decomposition + Y/N. Tap 'skip #N' to test."
echo
echo "  Read docs/setup-tutorial.md for the full walkthrough."
echo "  Read docs/operations-manual.md when something breaks."
echo
