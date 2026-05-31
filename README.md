# alfred-code

**Sir's lane-orchestrated, smoke-gated, fleet-aware Claude Code rig.**

A plug-and-play install of hooks, slash commands, subagent personas, and
Telegram-channel orchestration patterns. Codifies the workflow that ships
multi-lane work without losing functional verification.

## What it gives you

| Layer | Files | What it does |
|---|---|---|
| **Hooks** | `hooks/*.sh` | 6 deterministic guardrails. Block `env` dumps, block dead SSH aliases, force `git fetch` before reading, enforce worktree isolation on `Agent` dispatch, propose memory candidates on session end, require smoke-evidence in PR bodies. |
| **Slash commands** | `commands/*.md` | `/lane-out`, `/lane-smoke`, `/cut-release`, `/fleet-pull`, `/cleanup-memory`, `/ultrareview`. Each removes the boilerplate of a workflow you'd otherwise retype. |
| **Subagent personas** | `agents/*.md` | `lane-worker.md` + `lane-orchestrator.md`. Inherited persona files with all the repo conventions, smoke gates, and gotchas baked in once. |
| **Smoke templates** | `smoke-templates/*.md` | Copy-paste recipes for the five recurring smoke shapes — channel-token-route, workflow-trigger, migration-roundtrip, mcp-server-tool, wasp-op. |
| **GitHub workflows** | `workflows/*.yml` | Auto-PR-review (opt-in, API key), PR-review-gate (smoke-evidence required), deploy-fleet (auto-roll on `:latest` push). |
| **Cron + bridge** | `cron/*.sh`, `bridge/` | Native Claude Code Channel plugin install for Telegram; daily digest via Scheduled tasks. |
| **Docs** | `docs/` | Operations manual, decision-log convention, lane protocol formalised. |

## Two install paths

### A. Plugin install (recommended, when supported by your Claude Code version)

```bash
# Inside Claude Code:
/plugin install ssdavidai/alfred-code

# Hooks, commands, agents auto-merge into ~/.claude/
# Extras stay opt-in:
~/.claude/plugins/alfred-code/install-tier-2.sh
```

### B. Drop-in install (works on any Claude Code version)

```bash
git clone https://github.com/ssdavidai/alfred-code ~/.claude/alfred-code
~/.claude/alfred-code/install.sh
# symlinks hooks/ commands/ agents/ into your ~/.claude/
# merges settings.json.template into your settings.json
```

## Quick start: ship a real lane in 60 seconds

After install:

```bash
# In Claude Code, on any GitHub repo:
/lane-out 220     # reads issue #220, drafts lane decomposition, asks Y/N, dispatches
```

The `lane-out` command will:
1. Fetch the issue body + the open-PR landscape
2. Decompose per the lane protocol (`docs/lane-protocol.md`)
3. Define contracts (file ownership, shared schemas, env-var names)
4. Surface a 3-bullet "here's what I'll do" via `AskUserQuestion`
5. On `Y`: dispatch lane subagents in **isolated worktrees** (mandatory; the hook blocks it otherwise)
6. Each subagent inherits `agents/lane-worker.md` — all the boilerplate is gone
7. Smoke is the close gate, not lint

## Tier breakdown

| Tier | Setup time | What it gives you |
|---|---|---|
| 1 (install) | ~15 min | Hooks + commands + agents working in your terminal |
| 2 (Telegram) | ~1 evening | Text the bot from your phone → Claude reacts in your laptop terminal → all your slash commands and hooks apply |
| 3 (GitHub flow) | ~1 day | GitHub webhooks → Telegram notifications; daily digest via Scheduled tasks; fleet auto-rollout; PR-review-gate requires smoke evidence |

## Why this exists

Sir was shipping `#120 Multi-Profile Hermes` in 7 lanes across 3 hours and 22 PRs. Half the friction was retyping the same boilerplate in every subagent prompt. The other half was forgetting to add `isolation: "worktree"` until two agents collided on `migrate.ts`. This package codifies the lessons so the next 100 lanes go smoothly.

## Status

`v0.1.0` — Tier 1 implementation. Tested on `ssdavidai/alfred`.
