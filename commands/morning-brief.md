---
description: "Compose + push ONE Telegram digest of the loop's state: gates awaiting Y/N, PRs ready to merge, blocked decisions, stuck issues, staging health. Read-only; fires once a day."
allowed-tools: Bash(gh:*, curl:*, jq:*, cat:*, date:*), Read
argument-hint: "(no args)"
---

# Morning brief — one Telegram digest, no action taken

You are composing the **daily PM digest** for Sir. This is READ-ONLY — you do
not dispatch, merge, triage, or change any state. You gather what needs Sir's
attention and push exactly ONE Telegram message, then exit.

State lives in `~/.alfred-code-state/`; repo is `ALFRED_CODE_REPO` from
`~/.alfred-code-state/.env` (default `ssdavidai/alfred`). Bot creds via the
broker: `secret get telegram-bot-token` / `telegram-chat-id`.

## Gather (all read-only)

1. **Gates awaiting your Y/N** — `pending-gates.json` entries with
   `status="awaiting"`. These are triaged issues the loop wants to dispatch.
2. **Decisions you owe** — `pending-gates.json` entries with
   `status="approved-blocked"` (an epic that needs a call before it can build).
3. **PRs ready to merge** — open PRs whose body has `## Smoke evidence` AND a
   posted ultrareview verdict (check issue comments) — i.e. built + reviewed,
   waiting on your merge. `gh pr list --repo $REPO --state open --json number,title,headRefName`.
4. **In-flight builds** — `dispatched.json` entries with `status="building"`
   (note if any pid is dead = crashed) or `"pr-open"` (done, awaiting merge).
5. **Stuck** — `stuck.json` entries at/over the kill-criteria threshold
   (something failed 3×, needs your eyes).
6. **Counts** — open issue count; new issues opened since yesterday.
7. **Health** — is `staging.alfred.black` 200? (one curl, optional). Note if
   the poll heartbeat looks stale (`last-issue-poll` older than ~20 min during
   the day = Desktop/routine may be down).

## Compose ONE message (Telegram Markdown)

Keep it scannable — Sir reads this on his phone first thing. Shape:

```
☀️ *Alfred-code brief — <date>*

🆕 Awaiting your Y/N (N):
  • #203 cost-surface — scoped-feature, 3 lanes
  • #207 …

🛑 Decisions you owe (N):
  • #189 backup — restic sidecar vs Hetzner snapshots?

🟢 Ready to merge (N):
  • #218 ctrl skill routes — SHIP, real smoke
  • #217 skill UI — SHIP, staging-verified

🔨 Building: #206 (pid alive, 12m)
⚠️ Stuck: none
📊 7 open issues · heartbeat ok · staging 200

Reply y/n #N to gate · merge in the GH UI · ? to ask me anything.
```

Omit any section that's empty (don't print "Awaiting: none" — just drop it).
If EVERYTHING is empty (nothing awaiting, nothing ready, nothing stuck), send a
one-liner: `☀️ Alfred-code brief — all clear. N open issues, nothing needs you.`

## Send + exit

```bash
TG=$(secret get telegram-bot-token); CID=$(secret get telegram-chat-id)
curl -sS -X POST "https://api.telegram.org/bot${TG}/sendMessage" \
  --data-urlencode "chat_id=${CID}" \
  --data-urlencode "text=${MESSAGE}" \
  --data-urlencode "parse_mode=Markdown"
```

Then stop. Do not take any action on anything in the brief — that's Sir's call
during his checkpoint. Never echo secret values.
