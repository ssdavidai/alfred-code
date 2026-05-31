# Operations manual

How the autonomous loop works, what each piece does, and what to do when it breaks.

## The loop, at a glance

```
GitHub                                  Sir's Mac
issue filed ──→ notify-telegram.yml ──→ Telegram bot
                                        │
                                        ↓
                                        [Sir's interactive session: he can chat]
                                        
                Desktop scheduled task (every 5 min):
                  spawns: claude --print "/poll-and-act"
                          │
                          ↓
                  ~/.alfred-code-state/
                          │
                          ├─ Poll GH issues since last poll
                          ├─ Poll Telegram for Sir's replies
                          ├─ For new issues: triage + decompose + Telegram Y/N
                          ├─ For approved gates: dispatch /lane-out
                          ├─ For open PRs: check progress, post status
                          └─ For merged PRs: roll fleet (via GH Action)
```

## The state directory

`~/.alfred-code-state/` is the single source of truth between poll-and-act runs.

| File | Owner | Purpose |
|---|---|---|
| `.env` | Sir (via `/setup-telegram`) | Bot token + chat_id + repo |
| `last-issue-poll` | `/poll-and-act` | ISO timestamp; only newer issues are picked up |
| `last-tg-update-id` | `/poll-and-act` | Telegram update_id; only newer messages are read |
| `pending-gates.json` | `/poll-and-act` | Y/N gates awaiting Sir's tap |
| `dispatched.json` | `/poll-and-act` | Issues currently dispatched (with PR refs) |
| `stuck.json` | `kill-criteria.sh` | Issues that have hit the partial-iteration limit |
| `paused` | Sir (via "pause" in Telegram) | If present, the poll exits early |

**Safe to delete the whole directory** for a clean restart — `/setup-telegram` rebuilds everything.

## The seven commands

| Slash command | What it does | Who runs it |
|---|---|---|
| `/lane-out <issue#>` | Decompose + Y/N + dispatch lanes | Sir interactively OR `/poll-and-act` after approval |
| `/lane-smoke <kind>` | Emit a smoke template ready to paste | A lane worker writing its smoke |
| `/cut-release [tag]` | CHANGELOG + tag + GH Release | Sir at end of day |
| `/fleet-pull [services]` | SSH each tenant + pull | Sir to force a rollout |
| `/cleanup-memory` | Review queued memory candidates | Sir at end of session |
| `/ultrareview <pr#>` | 3 parallel reviewers (contract, smoke, code) | Sir before merging, OR `/poll-and-act` for PRs idle >30min |
| `/poll-and-act` | The autonomous heartbeat | Desktop scheduled task every 5 min |
| `/triage-issue <n>` | Single-issue triage | `/poll-and-act` per new issue |
| `/setup-telegram` | Guide Sir through the bot setup | Sir on first install |
| `/file-adr <title>` | File an Architecture Decision Record | After a load-bearing Y/N gate |
| `/pm-dashboard` | Emit markdown PM summary | Sir for a status check |

## The six hooks (deterministic guardrails)

| Hook | Event | Effect |
|---|---|---|
| `block-env-dump.sh` | PreToolUse(Bash) | Block `env` dumps |
| `block-dead-ssh-aliases.sh` | PreToolUse(Bash) | Block ssh to defunct hostnames |
| `force-fetch-before-read.sh` | UserPromptSubmit | `git fetch origin` per-hour |
| `enforce-worktree-isolation.sh` | PreToolUse(Agent) | Block lane dispatches missing `isolation: "worktree"` |
| `propose-memory-candidates.sh` | Stop | Scan transcript, queue lessons |
| `require-smoke-evidence.sh` | PreToolUse(Bash:gh pr merge) | Block merge w/o `## Smoke evidence` |
| `kill-criteria.sh` | PreToolUse(Agent) | Block dispatch after N partials on same issue |
| `token-budget-warn.sh` | UserPromptSubmit | Warn at token thresholds |

## The three GitHub Actions

| Workflow | Trigger | Effect |
|---|---|---|
| `notify-telegram.yml` | issue/PR/workflow_run events | Telegram message to Sir |
| `pr-review-gate.yml` | every PR | Required check: `## Smoke evidence` present |
| `deploy-fleet.yml` | `:latest` image push or manual | SSH each tenant + `docker compose pull && up -d` |

## The two scheduled tasks

| Task | Schedule | Spawns |
|---|---|---|
| `alfred-code-poll` | Every 5 min | `claude --print "/poll-and-act"` in the alfred repo's working folder |
| `alfred-code-digest` (optional) | Daily 8 AM | `claude --print "/pm-dashboard"` → Telegram |

## Troubleshooting

### "I filed an issue, nothing happened in Telegram"

1. Check the workflow ran: `gh run list --workflow=notify-telegram.yml --limit 3`
2. If it ran and failed: read the log, fix the secret if missing
3. If it didn't run: the workflow file isn't on `main`; merge it
4. If it ran and succeeded but you got no message: the `ALFRED_CODE_CHAT_ID` is wrong; re-run `/setup-telegram` step 2

### "Sir tapped Y but no dispatch happened"

1. Check the scheduled task's last run: open Desktop → Routines → alfred-code-poll → history
2. If the run was OK but the gate is still awaiting: the bot's `getUpdates` may have missed the reply because Sir's interactive session already consumed it (channels and bot polling share the same update queue!)
3. Workaround: have Sir send the same reply text directly (e.g. `dispatch #220`) so the next poll picks it up
4. Or use the long-term fix: configure the bot in `webhook` mode rather than `getUpdates` mode

### "A PR opened but no review happened"

1. Check `/poll-and-act`'s last run — was it during the PR opening?
2. If yes but no review: `/ultrareview` was likely blocked by the kill-criteria hook (3 partials on the same issue)
3. Clear the counter manually: `jq 'del(.["<issue>"])' ~/.alfred-code-state/stuck.json > /tmp/x && mv /tmp/x ~/.alfred-code-state/stuck.json`

### "Telegram bot stops responding"

1. Bot token rotated? Re-run `/setup-telegram` → step 3 to rewrite the .env
2. Channel paused? Check `claude --channels` was passed at launch; restart with the flag
3. Rate limited by Telegram? Each bot can send up to 30 messages/sec; we're far below this

### "Fleet rollout fails on one tenant"

1. Check the workflow log: `gh run view <id> --log`
2. Common: `FLEET_SSH_KEY` is wrong, or the tenant's `~/.ssh/authorized_keys` doesn't include the public half
3. Re-add: `ssh-copy-id -i ~/.ssh/alfred-black-verify.pub root@<host>.alfred.black`

## When to pause the loop

Sir texts the bot `pause` (or writes the file `~/.alfred-code-state/paused`).

Resume with `resume` (or `rm ~/.alfred-code-state/paused`).

Use cases:
- You're about to do destructive work and don't want the loop interfering
- You're on vacation and don't want gates piling up
- You're debugging the loop itself

## When to nuke and restart

```bash
rm -rf ~/.alfred-code-state
~/.claude/alfred-code/install.sh
# Then re-run /setup-telegram
```

This is safe; nothing under `~/.alfred-code-state/` is irrecoverable. The actual work (issues, PRs, commits) lives in GitHub.
