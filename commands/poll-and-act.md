---
description: "Autonomous-loop heartbeat: read new issues + Sir's recent Telegram replies, triage + decompose, dispatch on approval, verify completed PRs, post status."
allowed-tools: Bash(gh:*, curl:*, jq:*, date:*), Read, Write, Edit, Agent, TaskCreate, AskUserQuestion
---

# State directory

The autonomous loop tracks its own state under `~/.alfred-code-state/`:

- `last-issue-poll` — ISO timestamp of the last successful issue poll
- `last-tg-update-id` — Telegram `update_id` last consumed (so we don't double-act)
- `pending-gates.json` — Y/N gates waiting for Sir's tap
- `dispatched.json` — issues we've dispatched (with their gate IDs + lane PRs)

!`mkdir -p ~/.alfred-code-state && ls -la ~/.alfred-code-state/ 2>/dev/null`

# Config

The bot token + chat_id are in `~/.alfred-code-state/.env` (created by `/setup-telegram`):

!`cat ~/.alfred-code-state/.env 2>/dev/null | awk -F= '{print $1"=<set>"}' | head`

---

You are the **autonomous loop heartbeat**. This command runs every 5 min via a Desktop Scheduled Task. Each run is short and stateless beyond what's persisted under `~/.alfred-code-state/`.

If `~/.alfred-code-state/.env` is missing, **stop and tell Sir to run `/setup-telegram` first**.

## Process (run in order, stop after each step's work is done)

### 1. Poll Telegram for Sir's recent replies

```bash
TG_TOKEN=$(grep ^ALFRED_CODE_BOT_TOKEN= ~/.alfred-code-state/.env | cut -d= -f2)
LAST=$(cat ~/.alfred-code-state/last-tg-update-id 2>/dev/null || echo 0)
OFFSET=$((LAST + 1))
curl -sS "https://api.telegram.org/bot${TG_TOKEN}/getUpdates?offset=${OFFSET}&timeout=2" | jq .
```

For each new message: parse what Sir wants.

Sir's command vocabulary (case-insensitive). **`y`/`n` mean "do/don't do the
action that gate proposed"** — which is NOT always "dispatch". Read the gate's
`triage` field:
- For a `skip` gate, the proposed action is *close the issue*, so `y #N` → close it.
- For a `scoped-feature`/`epic`/`trivial-fix`/`research` gate, the proposed
  action is *dispatch*, so `y #N` → dispatch.
- Always phrase the gate's Telegram message to match (e.g. "Reply `y #N` to
  close it" for skip gates, "Reply `y #N` to dispatch" for work gates).

- `y`, `Y`, `👍` → **do the gate's proposed action** (dispatch, or close-if-skip). Mark the gate terminal.
- `n`, `N`, `👎` → **don't** do it; for work gates mark `skipped`, for skip gates leave the issue open and mark the gate `closed`.
- `?<question>` → respond with the answer; leave the gate open
- `status` or `status #N` → report current state
- `merge #N` → run `gh pr merge N --squash` (subject to the smoke-evidence hook)
- `skip #N` → close the gate for #N without dispatch
- `pause` → write `~/.alfred-code-state/paused` (the next poll exits early)
- `resume` → delete the pause file
- anything else → ignore (Sir's interactive Telegram session handles it)

After processing each message, update `~/.alfred-code-state/last-tg-update-id` with the highest `update_id` you saw.

### 2. Poll GitHub for new issues

```bash
SINCE=$(cat ~/.alfred-code-state/last-issue-poll 2>/dev/null || date -u -v-1H +%FT%TZ)  # default: last hour
gh issue list --state open --limit 30 --search "created:>$SINCE" --json number,title,body,labels,createdAt
```

**MANDATORY dedup guard — this is what stops the every-5-min spam.** Before
triaging or posting ANYTHING, load the state files and compute the set of
issue numbers already handled:

```bash
cat ~/.alfred-code-state/pending-gates.json 2>/dev/null | jq -r '.[] | select(.status != "closed" and .status != "skipped") | .issue'
cat ~/.alfred-code-state/dispatched.json    2>/dev/null | jq -r '.[].issue // empty'
```

For each candidate issue, **SKIP it entirely** (no triage, no Telegram post,
no gate write) if **either**:
- a gate already exists for that issue in `pending-gates.json` with a
  non-terminal status (`awaiting | approved | dispatching | dispatched`), or
- the issue already appears in `dispatched.json`.

The timestamp filter is only a coarse pre-filter. **The gate-existence check
is the authoritative dedup** — never rely on `last-issue-poll` alone, because
GitHub search time-rounding plus the stateless-per-run nature of this command
mean the same issue resurfaces across runs. The state files are the source of
truth, and you MUST persist the new gate to `pending-gates.json` in the SAME
run you post it (write the file *before* you exit, even if the Telegram send
is the last thing you do).

For each genuinely-new issue (no live gate, not dispatched): 

- **Triage** by reading the body. Categories:
  - `trivial-fix` — one-liner, no decomposition needed, dispatch single Lane I/II/III agent
  - `scoped-feature` — 1-3 lanes, draft a decomposition
  - `epic` — 4+ lanes; draft a decomposition AND warn Sir it's big
  - `research` — needs investigation before code; dispatch a research agent (sandbox, no PR)
  - `skip` — already covered by an open PR, or a duplicate, or out of scope
- **For non-skip**: draft a 3-bullet decomposition per the lane protocol (`docs/lane-protocol.md`)
- **Post to Telegram** with the decomposition + a Y/N gate

Telegram post shape:

```
🆕 Issue #220 needs your call:
<title>

Triage: scoped-feature (3 lanes)

  Lane I — ... | files: a, b, c | smoke: ...
  Lane II — ... | ...
  Lane III — ... | ...

Reply `y #220` to dispatch, `n #220` to skip, or `? <question>`.
```

Register the gate in `~/.alfred-code-state/pending-gates.json`:

```json
{
  "gate-220-<uuid>": {
    "issue": 220,
    "triage": "scoped-feature",
    "decomposition": "...",
    "tg_message_id": 12345,
    "status": "awaiting",
    "created_at": "2026-05-31T12:34:56Z"
  }
}
```

After processing, update `last-issue-poll` to now.

### 3. For each pending gate Sir approved in step 1 — dispatch

For each gate with `status="approved"`:

- Set `status="dispatching"` in the state file
- Invoke `/lane-out <issue#>` with the auto-dispatch flag (so it skips the AskUserQuestion since Sir already approved via Telegram)
- Set `status="dispatched"` after the lane-out command completes

### 4. For each issue currently `dispatched` — check PR landscape

For each issue in `dispatched.json`:

```bash
gh pr list --state open --search "in:body #<issue>" --json number,title,headRefName,mergeable
```

If a new PR appeared since last poll:
- Post to Telegram: "🟢 PR #<n> opened for #<issue>: <title>. Reply `review #<n>` for an ultrareview."

If a PR has been open for >30 min with no body update:
- Run `/ultrareview <n>` autonomously and post the result to Telegram

If all of the issue's lanes have merged:
- Verify the issue's acceptance criteria are met
- Auto-close the issue with a summary comment
- Mark the gate `status="closed"`

### 5. House-keeping

- Delete expired gates (older than 24h, not approved)
- Trim `pending-gates.json` and `dispatched.json` to the last 30 days

## Honest reporting rules

- **Don't dispatch without Sir's approval.** Every issue requires an explicit Y/N from Sir, even if you're 99% sure.
- **Don't merge a PR without smoke evidence.** The `require-smoke-evidence.sh` hook will block you anyway.
- **Surface partials honestly.** If a lane came back partial, post that to Sir, don't pretend it shipped.
- **Don't spam.** If nothing new happened, exit quietly — no "all clear" messages every 5 min.

## What to do if you hit a hard error

- Telegram API errors: log + retry next iteration (transient)
- GitHub API rate limit: log + sleep + retry next iteration  
- Lane dispatch fails: post the error to Telegram with the gate id; don't auto-retry
- State file corrupted: back up, re-init, post warning to Telegram
