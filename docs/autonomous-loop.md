# The autonomous loop — architecture

The whole alfred-code system is one loop. Here's the architecture in one diagram + the rationale for each piece.

```
                    ┌────────────────────────────────────────────┐
                    │             GitHub (the source of work)    │
                    │  ssdavidai/alfred                          │
                    └──────┬─────────────────────────────────────┘
                           │
                           │ events: issues, PRs, workflow_run
                           ↓
                    ┌──────────────────────────────────────────┐
                    │  GitHub Actions (free, $0)               │
                    │  - notify-telegram.yml                   │
                    │  - pr-review-gate.yml (required check)   │
                    │  - deploy-fleet.yml                      │
                    └──────┬─────────────────────────────────────┘
                           │
                           │ Telegram Bot API (free)
                           ↓
                    ┌──────────────────────────────────────────┐
                    │  Telegram bot @alfred_code_bot           │
                    │  (managed by BotFather; free)            │
                    └──────┬───────────────────────────────┬───┘
                           │                               │
                           │ push notifications             │ Sir's replies
                           ↓                               ↑
                    ┌──────────────────────────────────────────┐
                    │  Sir's Mac (laptop, must be awake)       │
                    │                                          │
                    │  ┌──────────────────────────────────┐   │
                    │  │ Claude Code Desktop              │   │
                    │  │                                  │   │
                    │  │ - Interactive session (Sir chats)│   │
                    │  │   running with --channels        │   │
                    │  │   plugin:telegram@…              │   │
                    │  │                                  │   │
                    │  │ - Scheduled task: alfred-code-   │   │
                    │  │   poll, fires every 5 min,       │   │
                    │  │   runs /poll-and-act in an       │   │
                    │  │   isolated git worktree          │   │
                    │  │                                  │   │
                    │  └──────────────────────────────────┘   │
                    │                                          │
                    │  ~/.alfred-code-state/                   │
                    │  ├─ .env (bot token, chat_id)            │
                    │  ├─ last-issue-poll                      │
                    │  ├─ last-tg-update-id                    │
                    │  ├─ pending-gates.json                   │
                    │  ├─ dispatched.json                      │
                    │  └─ stuck.json                           │
                    └──────┬─────────────────────────────────────┘
                           │
                           │ subagent dispatch (Agent tool)
                           ↓
                    ┌──────────────────────────────────────────┐
                    │  Lane workers (isolated git worktrees)   │
                    │  Each inherits agents/lane-worker.md     │
                    │  Smoke evidence required to merge        │
                    └──────┬─────────────────────────────────────┘
                           │
                           │ PRs back to GitHub
                           └────────────────────────────→ (loop closes)
```

## Each layer's job

### Layer 1: GitHub (the truth)

The actual repo. Source of issues, source of PRs, source of CI signal. Nothing in alfred-code replaces this — we're a coordination layer ON TOP.

### Layer 2: GitHub Actions (the free notification + gate layer)

Three workflows:

1. **`notify-telegram.yml`** — observes events, posts to Telegram. Zero state, zero compute, zero cost. Just an event filter + a Telegram API call.

2. **`pr-review-gate.yml`** — required check. Parses the PR body, asserts `## Smoke evidence` is present. Lint-pass-shipping is the failure mode this prevents.

3. **`deploy-fleet.yml`** — fires on `:latest` push (which happens when a build workflow completes). SSHes to each tenant, pulls, restarts. No Claude in the loop; SSH-based deployment.

### Layer 3: Telegram bot (the always-on surface)

The bot is free. It's accessible from Sir's phone, laptop, anywhere. It survives Sir's laptop being asleep (messages queue; Sir sees them next time he opens Telegram). It's the **one channel Sir checks**.

The bot has two "personalities" depending on context:
- **Outbound** (from notify-telegram.yml or from the scheduled task): "Hey, X happened, want to act?"
- **Inbound** (Sir's chat with the bot via channels plugin): Sir asks Claude things, Claude answers

### Layer 4: Sir's Mac

This is the only must-be-on machine. Anthropic-side is free (subscription); the laptop itself is free (Sir already owns it); the work happens here.

Two Claude things run here:

1. **Interactive session** — `claude --channels plugin:telegram@claude-plugins-official` in a persistent terminal. Sir asks questions, Claude answers via the bot. Stays up while Sir's working.

2. **Scheduled task `alfred-code-poll`** — fires every 5 min via macOS launchd (managed by Claude Code Desktop). Spawns a fresh, ephemeral session that runs `/poll-and-act`. Each run has its own git worktree, so the running interactive session is unaffected.

### Layer 5: State (`~/.alfred-code-state/`)

The single source of truth between poll-and-act runs. Sqlite would be overkill; JSON files are enough. Atomic writes via `tempfile + mv`.

### Layer 6: Lane workers

When `/poll-and-act` dispatches a lane after Sir's 👍, each lane worker is a subagent in its own git worktree. The `enforce-worktree-isolation.sh` hook ensures this. The `lane-worker.md` persona codifies the conventions (smoke required, audit writes, etc).

## What's deterministic vs agentic at each layer

| Layer | Deterministic | Agentic |
|---|---|---|
| GitHub | Storage, webhooks | (none) |
| GH Actions | Workflow YAML, secret handling | (none — pure CI/CD) |
| Telegram bot | Send/receive HTTP API | (none) |
| Scheduled task trigger | macOS launchd cron | (none) |
| `/poll-and-act` body | State file I/O, Telegram polling, GH polling | Triage decisions, lane decomposition |
| Lane workers | Git operations, file writes, CI invocation | Code generation, smoke design |
| Hooks | All deterministic | (none) |
| `pr-review-gate` check | Regex on PR body | (none) |
| `/ultrareview` | (none) | 3 reviewer agents in parallel |
| `deploy-fleet` SSH+pull | SSH + docker compose | (none) |

The pattern: **deterministic outside, agentic inside**. The shell is rigid; the contents are reasoned about.

## Failure modes the architecture protects against

| Failure | What protects | How |
|---|---|---|
| Sir's laptop asleep | Telegram queue | Messages persist; Sir sees them when laptop wakes |
| Sir misses a notification | Daily digest | Morning summary lists overnight activity |
| Lane worker writes wrong file | Worktree isolation hook | Each lane's edits are scoped to its tree; merge conflicts surface at PR time |
| PR with no smoke evidence | `pr-review-gate.yml` | Required check fails; can't merge |
| Lane keeps coming back partial | `kill-criteria.sh` hook | After 3 partials, dispatch blocks until Sir intervenes |
| Stale local tree → mis-diagnosis | `force-fetch-before-read.sh` hook | `git fetch origin` per-hour cache |
| Accidental ssh to dead alias | `block-dead-ssh-aliases.sh` hook | Blocks ssh to defunct hostnames |
| Env dump leaks secrets | `block-env-dump.sh` hook | Blocks bare `env` |
| Session token spend runaway | `token-budget-warn.sh` hook | Warns at thresholds |
| GitHub Actions misconfigured | `notify-telegram.yml` fires on workflow_run failure | Sir sees the failure in Telegram |
| Fleet rollout fails on one tenant | `deploy-fleet.yml` step exits non-zero | Logged, Telegram summary shows which tenant failed |
| Bot token rotated | `/setup-telegram` re-pair | One-command repair |

## What this architecture is NOT

- **Not a CI replacement.** GitHub Actions still runs the actual build/test pipeline. Alfred-code coordinates around the CI signal, doesn't replace it.
- **Not a chat bot.** The Telegram bot is for status updates + Y/N gates, not for free-form chat with Claude (use the interactive session for that).
- **Not multi-tenant.** It's Sir-the-individual's coordination layer for Sir's repos. Adding multi-tenant is a future-project concern.
- **Not always-on without a laptop.** macOS-side it requires Sir's machine awake. For 24/7 operation, you'd swap the Desktop scheduled task for Anthropic's Cloud Routines (which DO cost API tokens).

## Future possibilities

- Build a custom Slack channel plugin so the same loop works in Slack workspaces
- Use Cloud Routines for 24/7 operation when Sir's not at his desk (with a token budget)
- Replace `/ultrareview` with parallel Sonnet calls instead of Opus for cost optimization
- Add a `/sir-mode` toggle that puts the loop into "I'm working alongside you" rather than "I'm autonomous"
