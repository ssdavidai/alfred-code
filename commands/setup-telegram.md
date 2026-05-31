---
description: "Walk Sir through the BotFather → /plugin install → /telegram:configure flow + write state files."
allowed-tools: Bash(curl:*, mkdir:*, ls:*), Read, Write, AskUserQuestion
---

# Current state

!`mkdir -p ~/.alfred-code-state && ls -la ~/.alfred-code-state/ 2>/dev/null`

!`bun --version 2>/dev/null || echo "Bun NOT INSTALLED — install at https://bun.sh first; the telegram channel plugin needs it"`

!`claude --version 2>/dev/null | head -1`

---

You are guiding Sir through the one-time Telegram setup for alfred-code. The flow has 5 steps; surface them one at a time via `AskUserQuestion` so Sir can confirm each.

## Step 1 — BotFather: create the bot

Walk Sir through this:

1. Open Telegram, search for `@BotFather`
2. Send `/newbot`
3. Set display name to "Alfred Code"
4. Set unique username ending in `bot` (e.g. `alfred_code_bot` or `sir_alfred_code_bot`)
5. Copy the **bot token** BotFather returns (looks like `1234567890:AAEx_-…`)

Then ask Sir via `AskUserQuestion`:

```
Question: "Have you created the bot and copied the token?"
Options: ["Yes — pasting it now", "I'll do it later"]
```

If "Yes": prompt Sir for the token (he pastes it in his next message). **Don't echo it back.** Just confirm "got it, ~46 chars" and proceed.

## Step 2 — Get Sir's chat_id

To know where to send notifications, the system needs Sir's chat_id with the bot.

Walk Sir through:
1. Open Telegram, find your new bot
2. Send any message (e.g. "hi")

Then run on Sir's behalf:

```bash
TG_TOKEN="<token from step 1>"
curl -sS "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" | jq '.result[].message.chat.id' | head -1
```

The `chat.id` is what you want. Confirm with Sir before storing.

## Step 3 — Write state files

Create `~/.alfred-code-state/.env`:

```
ALFRED_CODE_BOT_TOKEN=<token>
ALFRED_CODE_CHAT_ID=<chat_id>
ALFRED_CODE_REPO=ssdavidai/alfred
```

Set perms `0600`:
```bash
chmod 600 ~/.alfred-code-state/.env
```

Also initialise the state directory:
```bash
echo 0 > ~/.alfred-code-state/last-tg-update-id
date -u +%FT%TZ > ~/.alfred-code-state/last-issue-poll
echo '{}' > ~/.alfred-code-state/pending-gates.json
echo '{}' > ~/.alfred-code-state/dispatched.json
```

## Step 4 — Install the Telegram channel plugin

Tell Sir to run, in his interactive Claude Code session:

```
/plugin install telegram@claude-plugins-official
```

If Claude says the plugin isn't found:
```
/plugin marketplace add anthropics/claude-plugins-official
/plugin marketplace update claude-plugins-official
/plugin install telegram@claude-plugins-official
```

Then:
```
/reload-plugins
/telegram:configure <token>
```

## Step 5 — Restart Claude with channels enabled

The plugin only polls Telegram when Claude was launched with `--channels`. So Sir needs:

```bash
# Quit current Claude session, then:
claude --channels plugin:telegram@claude-plugins-official
```

For convenience, add to `~/.zshrc`:
```bash
alias claude-tg='claude --channels plugin:telegram@claude-plugins-official'
```

## Step 6 — Pair + allowlist

Once Claude restarts:
1. In Telegram, send any message to the bot
2. The bot replies with a pairing code
3. In Claude Code, run: `/telegram:access pair <code>`
4. Lock down access: `/telegram:access policy allowlist`

## Step 7 — Verify

Send a test message via Telegram. Claude should reply through the bot.

Then test the outbound side:
```bash
TG_TOKEN=$(grep ^ALFRED_CODE_BOT_TOKEN= ~/.alfred-code-state/.env | cut -d= -f2)
TG_CHAT=$(grep ^ALFRED_CODE_CHAT_ID= ~/.alfred-code-state/.env | cut -d= -f2)
curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TG_CHAT}" \
  --data-urlencode "text=✅ alfred-code outbound test"
```

Sir should see "✅ alfred-code outbound test" in Telegram.

## Final step — set up the Desktop scheduled task

After Telegram is working:

1. Open the Claude Code Desktop app
2. Click **Routines** in the sidebar
3. Click **New routine** → **Local**
4. Fill in:
   - Name: `alfred-code-poll`
   - Description: "Autonomous-loop heartbeat for alfred-code"
   - Instructions: `/poll-and-act`
   - Working folder: `/Users/ssd/dev/alfred` (or wherever the repo you want monitored lives)
   - **Enable the worktree toggle** for isolation
   - Schedule: ask Claude in the Desktop session "set this task to run every 5 minutes"
5. Click **Run now** to test
6. Approve permission prompts; the task will auto-approve future runs

OR — the scheduled task SKILL.md can be installed from this repo directly:

```bash
cp ~/.claude/alfred-code/cron/alfred-code-poll.SKILL.md \
   ~/.claude/scheduled-tasks/alfred-code-poll/SKILL.md
mkdir -p ~/.claude/scheduled-tasks/alfred-code-poll
```

Then open Desktop → Routines and the task appears; configure schedule (Hourly / Daily / or custom "every 5 min" via Claude prompt).

---

When done, write a one-line confirmation to Sir: "Telegram is wired. Autonomous loop is live. File an issue and tap Y to test."
