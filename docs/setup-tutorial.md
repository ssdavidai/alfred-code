# Setup tutorial — from zero to autonomous in 30 minutes

This is the complete walkthrough. Follow it top to bottom and at the end you'll have:

- A Telegram bot that pings you when a GitHub issue or PR lands
- A scheduled task that polls your issues every 5 min, triages new ones, posts decompositions to Telegram, and dispatches lane workers when you tap 👍
- A `/ultrareview` flow that runs a 3-agent code review on PRs and posts the verdict
- Fleet rollout that fires automatically when you merge to main

Total Anthropic spend: **$0/month** — uses your Claude.ai subscription on your Mac.

## Prereqs

- macOS (Linux works too, replace `launchd` with `systemd` notes inline)
- Claude Code Desktop **v2.1.80 or later** (the Channels feature)
- `bun` installed (the Telegram channel plugin needs it) — `curl -fsSL https://bun.sh/install | bash`
- `gh` CLI authenticated against your GitHub account
- `jq` installed
- Your repo (e.g. `ssdavidai/alfred`) cloned somewhere you'll work from

## Phase 0 — Install Tier 1 (5 min)

```bash
# Clone alfred-code
git clone https://github.com/ssdavidai/alfred-code ~/.claude/alfred-code

# Drop-in install: symlinks hooks/commands/agents into your ~/.claude/
~/.claude/alfred-code/install.sh

# Verify
ls -la ~/.claude/hooks/
ls -la ~/.claude/commands/
ls -la ~/.claude/agents/

# In a Claude Code session, the new slash commands should be available:
# /lane-out /lane-smoke /cut-release /fleet-pull /cleanup-memory /ultrareview
```

Test a hook by trying a bare `env` in any session — it should refuse with an error.

## Phase 1 — Create the Telegram bot (5 min)

1. Open Telegram on your phone
2. Search for `@BotFather`, tap to open a chat
3. Send `/newbot`
4. BotFather asks for a display name → `Alfred Code`
5. Then asks for a unique username (must end in `bot`) → e.g. `your_alfred_code_bot`
6. BotFather replies with a token: `1234567890:AAEx_...` (looks ~46 chars)
7. **Copy the token**. Treat it like a password.

Now send `hi` to your new bot. The bot won't reply yet (we haven't paired it), but Telegram now has a chat for you to use.

Run this on your Mac to find your chat_id:

```bash
TG_TOKEN="<paste your token>"
curl -sS "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" | jq '.result[].message.chat.id' | tail -1
```

The output is a number like `987654321`. **Copy that too**.

## Phase 2 — Write the state files (2 min)

```bash
mkdir -p ~/.alfred-code-state
cat > ~/.alfred-code-state/.env <<EOF
ALFRED_CODE_BOT_TOKEN=<paste your bot token>
ALFRED_CODE_CHAT_ID=<paste your chat_id>
ALFRED_CODE_REPO=ssdavidai/alfred
EOF
chmod 600 ~/.alfred-code-state/.env

# Initialise the state directory
echo 0 > ~/.alfred-code-state/last-tg-update-id
date -u +%FT%TZ > ~/.alfred-code-state/last-issue-poll
echo '{}' > ~/.alfred-code-state/pending-gates.json
echo '{}' > ~/.alfred-code-state/dispatched.json
```

Verify outbound:

```bash
TG_TOKEN=$(grep ^ALFRED_CODE_BOT_TOKEN= ~/.alfred-code-state/.env | cut -d= -f2)
TG_CHAT=$(grep ^ALFRED_CODE_CHAT_ID= ~/.alfred-code-state/.env | cut -d= -f2)
curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TG_CHAT}" \
  --data-urlencode "text=alfred-code setup test ✓"
```

You should see "alfred-code setup test ✓" in your Telegram chat.

## Phase 3 — Install the Telegram channel plugin in Claude Code (3 min)

In Claude Code (the CLI or Desktop app, your choice):

```
/plugin marketplace add anthropics/claude-plugins-official
/plugin marketplace update claude-plugins-official
/plugin install telegram@claude-plugins-official
/reload-plugins
```

Then configure it with the same token:

```
/telegram:configure <paste your bot token>
```

Save it permanently by re-launching Claude with channels enabled:

```bash
# Quit current Claude session, then:
claude --channels plugin:telegram@claude-plugins-official
```

Add this alias to your `~/.zshrc` so it's easy:

```bash
alias claude-tg='claude --channels plugin:telegram@claude-plugins-official'
```

## Phase 4 — Pair + allowlist (1 min)

In your fresh `claude-tg` session:

```
# Send any message to your bot from Telegram
# The bot replies with a pairing code (in the Telegram chat AND your terminal)
/telegram:access pair <code from the bot>

# Then lock down so only you can reach the bot:
/telegram:access policy allowlist
```

Now test: send "what's the current branch?" to your bot from Telegram. Claude in your terminal session should answer (the reply lands in your Telegram chat).

## Phase 5 — Configure GitHub Actions (5 min)

In your **target repo** (e.g. `ssdavidai/alfred`):

```bash
# Add the workflows
cp ~/.claude/alfred-code/workflows/notify-telegram.yml .github/workflows/
cp ~/.claude/alfred-code/workflows/pr-review-gate.yml .github/workflows/
cp ~/.claude/alfred-code/workflows/deploy-fleet.yml .github/workflows/

# Add the required secrets
gh secret set ALFRED_CODE_BOT_TOKEN     # paste bot token
gh secret set ALFRED_CODE_CHAT_ID       # paste chat_id
gh secret set FLEET_SSH_KEY < ~/.ssh/alfred-black-verify   # private key

# Optional: list of fleet hosts (default is sensible)
gh variable set ALFRED_FLEET_HOSTS --body "home,rj,joe,zsolt,miguel,rami"

# Commit + push the workflows
git add .github/workflows/notify-telegram.yml .github/workflows/pr-review-gate.yml .github/workflows/deploy-fleet.yml
git commit -m "ci: alfred-code workflows" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push
```

Test: file a junk issue in the repo. Within ~10 seconds you should get a Telegram message:

> 🆕 *Issue #N* opened by ssdavidai
> Junk issue for testing
> https://github.com/ssdavidai/alfred/issues/N

Close the issue — you should get another Telegram ping for the close.

## Phase 6 — Make `pr-review-gate` a required check (1 min)

In GitHub:

1. Repo Settings → Branches → `main` (or your default branch)
2. Edit branch protection (or add it)
3. Under "Require status checks before merging", add `smoke-evidence-check`
4. Save

Now any PR opened without `## Smoke evidence` in the body cannot be merged.

## Phase 7 — Set up the autonomous loop scheduled task (5 min)

This is the heart. The desktop scheduled task runs every 5 min and runs `/poll-and-act`.

**Option A — via the Claude Code Desktop UI**:

1. Open the Claude Code Desktop app
2. Click **Routines** in the sidebar
3. **New routine** → **Local**
4. Fill in:
   - Name: `alfred-code-poll`
   - Description: "Autonomous-loop heartbeat for alfred-code"
   - Instructions: `/poll-and-act`
   - Working folder: `/Users/ssd/dev/alfred` (your repo)
   - Worktree toggle: **ON** (each run gets an isolated git worktree)
   - Permission mode: **Auto-approve** (you'll approve tool prompts once, then it auto-approves future runs)
   - Schedule: Hourly OR ask Claude in the Desktop session "set this task to run every 5 minutes"
5. Click **Run now** to test
6. Approve the permission prompts that appear; select "Always allow"

**Option B — via the SKILL.md file** (faster if you're a CLI person):

```bash
mkdir -p ~/.claude/scheduled-tasks/alfred-code-poll
cp ~/.claude/alfred-code/cron/alfred-code-poll.SKILL.md \
   ~/.claude/scheduled-tasks/alfred-code-poll/SKILL.md
```

Then open Desktop → Routines, the task appears; configure the schedule there.

## Phase 8 — End-to-end test (5 min)

The full loop should now be live. File a real test issue:

```bash
gh issue create --title "Test: alfred-code end-to-end" --body "This is a test of the autonomous loop. Triage as 'skip' and close."
```

Within ~10 seconds: Telegram pings you about the new issue.

Within ~5 minutes (next `/poll-and-act` tick): Claude on your Mac wakes up via the desktop scheduled task, runs `/poll-and-act`, picks up the new issue, runs `/triage-issue`, posts a decomposition + Y/N to Telegram.

Reply `skip #N` (or `n #N`) in Telegram. Next poll cycle: the gate closes, the loop notices the resolution, no dispatch happens.

If you reply `y #N` (or `dispatch #N`) instead: the next poll runs `/lane-out N`, which decomposes + dispatches lane workers in isolated worktrees on your Mac.

Each lane worker opens a PR; `notify-telegram.yml` pings you when each one opens.

Reply `review #PR` in Telegram → `/ultrareview` runs → review verdict posted.

Tap merge in GitHub UI → `pr-review-gate.yml` checks the smoke evidence is present → merge → `deploy-fleet.yml` rolls all 6 tenants → Telegram summary.

**That's the full loop.**

## Phase 9 — Daily digest (optional, 2 min)

If you want a morning summary in Telegram:

1. Desktop → Routines → New routine → Local
2. Name: `alfred-code-digest`
3. Instructions: `/pm-dashboard` (then ask Sir in the session to push the result to Telegram)
4. Schedule: Daily at 8:00 AM
5. Working folder: your repo

## Honest expectations

- **The first day will feel weird.** Sir's used to interactive Claude. Now Claude is silently doing work while Sir sleeps. The Telegram digest at 8 AM will help bridge that.
- **The 5-min interval is conservative.** If you want faster responsiveness, change the scheduled task to run every 1 min. The cost is the same.
- **Sir tapping wrong in Telegram can dispatch a wrong lane.** This is why the `kill-criteria.sh` hook exists — after 3 partials on the same issue, the system pauses and asks for direction.
- **The loop is interruptible.** Send `pause` in Telegram, the loop exits early. Send `resume` to start again. `rm -rf ~/.alfred-code-state` if you want to wipe and re-pair.

## What can still go wrong

See `docs/operations-manual.md` for the troubleshooting tree. Common issues:

- Bot token rotated → re-run `/setup-telegram` step 3
- Sir's interactive session ate the Telegram update → resend the reply explicitly
- A lane worker comes back partial → re-dispatch with the gap named (or, if 3 partials, kill-criteria hook fires)
- Fleet rollout fails on one tenant → `gh run view <id> --log`; usually missing SSH key

## What's next

Tier 3 polish items (not required for the loop to work):

- `/file-adr` for Architecture Decision Records on Y/N gates
- Token budget warnings
- The PM dashboard slash command (described above)
- Custom hooks for your specific repo's gotchas

These all live in this repo; add as you discover need.
