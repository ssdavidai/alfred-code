# Alfred Code

Alfred Code v2 is a deterministic GitHub control plane with Superset execution for `ssdavidai/alfred`. New issues enter an agent-silent draggable Backlog. The operator orders Inbox, selects Sprint Queue, explicitly starts a sprint, reviews a live-code-backed Fibonacci estimate and lane plan, and approves or rejects that exact immutable plan. Isolated Superset agents then build and independently review each lane. You remain the product manager and final merge authority.

GitHub owns work, approvals, PRs, CI, and merge state. The target repository's live `scripts/hooks/lanes.json` owns write boundaries. Superset owns isolated workspaces and agent sessions. SQLite owns durable scheduling, one-agent-per-lane leases, deduplication, observations, and immutable events. Slack is optional notification output, never a control protocol.

```text
GitHub Backlog -> ordered Inbox -> explicit Sprint Queue commitment
             -> SHA-pinned estimate and validated plan -> exact GitHub approval
             -> SQLite lane scheduler -> Superset workers
             -> GitHub PR + CI -> exact-HEAD independent review
             -> GitHub Project "Ready to merge" -> human merge
```

No agent can start without a current validated plan and exact approval authored by the immutable trusted GitHub operator `ssdavidai`. Comments from every other account are retained only as untrusted GitHub data: they cannot approve, reject, change planning context, suppress controller markers, or satisfy independent review. Configuration cannot widen this boundary, and the controller refuses to write comments unless `gh` is authenticated as `ssdavidai`. The controller does not merge, reset, force-push, delete branches, expose secrets, or write outside the approved lane. Workspace cleanup is disabled by default.

Workers and reviewers run only through the dedicated `Alfred Claude (Scoped)` and `Alfred Codex (Scoped)` Superset presets. The launcher rejects YOLO, bypass, alternate-sandbox, extra-directory, MCP, plugin, and configuration-override arguments. Codex receives a generated offline permission profile that makes the repository read-only except for the approved lane paths, ignored verification outputs, and one result marker. Before launch, the wrapper asks the local Codex app server for the exact hash of its lane guard and persists that hash only in the ephemeral profile; if the hook cannot be proven active, launch fails closed without a hook-trust bypass flag. Read-only toolchain, Git metadata, and validated dependency-cache targets are explicit grants, while environment secrets and home-directory credentials stay unreadable. Claude runs with strict native sandboxing, home-directory reads denied, no network or unsandboxed escape, and the same pre-tool lane guard. Both agents are forbidden from committing, pushing, or calling GitHub; after a result marker appears and the provider has exited, the controller independently checks the immutable `.lane` manifest, exact base or review SHA, every changed path, and the absence of deletions before it performs any Git or GitHub delivery. Any drift is quarantined.

The scoped launcher requires Python 3.11 or newer and atomically writes `.alfred-code-launch.json` before starting the provider. The controller treats that marker and the required result marker as process evidence; a Superset workspace by itself never proves that an agent is running. Launch failures are reported with their real exit reason, while a separate progress timeout catches a live agent that produces no repository changes.

## Install v2 safely

```bash
git clone https://github.com/ssdavidai/alfred-code ~/.claude/alfred-code
cd ~/.claude/alfred-code
./install-controller.sh
~/.claude/bin/alfred-code agents-provision
~/.claude/bin/alfred-code doctor
```

Installation creates `~/.config/alfred-code/controller.toml` with `apply = false`. It renders but does not load the launchd service. Follow [the v2 deployment runbook](docs/control-plane-runbook.md) to authenticate Superset, create the GitHub Project, import legacy evidence, audit worktrees, and deliberately enable execution.

The principal read-only commands are `alfred-code doctor`, `alfred-code status`, `alfred-code sprint-status`, `alfred-code run-once --dry-run`, and `alfred-code worktrees-audit`. External mutation is explicit through `project-setup`, `sprint-start`, `plan <issue> --publish`, the loopback dashboard's nonce-bound Start Sprint and Split Issue actions, or an apply-enabled reconciliation cycle.

## Local operations dashboard

Alfred Operations is a loopback-only dashboard over the durable controller database, append-only events, Superset workspace/session bindings, and persisted Codex/Claude token telemetry. It shows the complete Kanban, active sprint commitment and additions, velocity, carryover, tokens per delivered point, every active safe planner process, every materialized lane job and lease, PR links, block reasons, plan churn, model usage, per-issue token economics, and the live activity feed. Its two bounded write actions start the cards already placed in Sprint Queue and explicitly split a 21-point Needs Splitting card. Both require a per-process nonce obtained through same-origin snapshot access, and the server refuses non-loopback binding.

Run it for the current terminal:

```bash
~/.claude/bin/alfred-code dashboard --open
```

Install it as an always-on macOS launch agent:

```bash
cd ~/.claude/alfred-code
./install-dashboard.sh
open http://127.0.0.1:7331
```

The page refreshes every two seconds. Build and review token totals are recovered from the exact persisted Superset-bound CLI sessions. Planner model and token telemetry is captured from Codex's JSONL event stream. Starting a sprint freezes ordered commitment; later additions are tracked separately. The controller runs up to `max_parallel_planners` Codex `gpt-5.6-sol` planners concurrently, assigns validated Fibonacci points with evidence, and continues advancing approved jobs while specifications run. Ordinary Backlog and Inbox cards never invoke a planner. The sole Inbox exception is work returned from a closed sprint because it was blocked: the controller retires its failed execution graph and generates one fresh plan for refinement, but cannot materialize or launch any replacement job there. Planner invocations are ephemeral, schema-constrained, web-disabled, and forced into an Alfred-owned read-only permission profile. Superset workers remain exclusive per lane, but independent lanes run concurrently; the highest-priority runnable sprint work gets each available lane.

Plans include the issue-body hash, trusted-operator-feedback context hash, pinned base SHA, Fibonacci story points, estimate evidence, and cross-issue dependencies. A 21-point plan is sent to Needs Splitting and has no approval path. Its dashboard drawer explains the estimate and previews one proposed child per bounded plan job; nothing is created until the operator clicks Split. The durable split then creates native GitHub sub-issues in Inbox, resumes safely after partial failure, and leaves the parent open as the epic. Children remain agent-silent until the operator prioritizes them into a sprint. Regeneration, issue edits, trusted `ssdavidai` feedback, and pre-approval base changes invalidate approval. An exact `/reject-plan` decision from `ssdavidai` is durable and starts no workers. The planner receives controller-collected git evidence and may inspect only the current checkout through its `alfred-planner` Codex permission profile; it cannot edit the repository, read credential-shaped files, use the network, or load MCP servers, plugins, and memories. Any verification command it returns is discarded and replaced by the live lane policy's command. Every lifecycle transition refreshes GitHub or Superset first; an unavailable authority freezes advancement. The controller fetches the full PR file list and rejects scope escapes before review. Independent review is accepted only from `ssdavidai`, after review was requested, for the current PR HEAD, after green CI and smoke evidence. A failed review launches at most two exact-SHA, nonce-bound repair attempts in the original scoped workspace; the trusted controller alone validates, commits, and pushes each repair before a new CI and review cycle. Deterministic workspace names plus intent-before-effect records adopt accepted work after a crash instead of launching duplicates.

Approved execution can automatically return to specification only for recognized lane-authority, contract-plan, or base-conflict blockers, and only after every current job is quiescent. Merged jobs remain immutable history; unfinished jobs become `superseded`. Controller-owned conflicting PRs are closed with an audit comment while their branches, commits, and Superset workspaces remain intact. Replacement jobs, branches, workspaces, and plan hashes receive a new revision identity, the old approval is revoked, and no replacement worker starts until the operator approves the full new hash. CI failures, failed verification, security quarantines, scope violations, exhausted repair loops, and unknown blockers never auto-replan. The loop is capped by `auto_replan_max_attempts` (two by default).

When a sprint closes, its membership remains immutable history. Done work stays Done. A terminally blocked item is moved to the top of Inbox, not into another sprint; its old approval and unfinished jobs are superseded, and any open PR must be proven controller-owned before the controller closes it non-destructively. The Inbox carryover flag causes exactly one new planning attempt. The fresh plan may be reviewed, revised, approved, or rejected in Inbox, but even an approved plan cannot create jobs or agents until the operator explicitly moves the issue to Sprint Queue and starts a future sprint. A planning failure clears the carryover flag instead of creating an unbounded token loop.

Read [the executable v2 architecture](docs/control-plane-v2.md) for the full authority and state model. Run the test suite with:

```bash
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Legacy v1 Telegram package

The material below describes the retained v1 Telegram/JSON loop. It is available for compatibility and migration evidence, but it is not the v2 lifecycle authority. `alfred-code migrate-legacy` fingerprints old JSON and logs without deleting them or trusting their status strings.

**Legacy autonomous-loop Claude Code rig. File a GitHub issue, tap Y in Telegram, the work ships.**

```
                        ┌──────────────────────────────────────────┐
                        │  You file a GH issue (~30s)              │
                        └──────┬─────────────────────────────────────┘
                               │
                               ↓
                        ┌──────────────────────────────────────────┐
                        │  Within ~5 min:                          │
                        │  • Triaged                               │
                        │  • Lanes decomposed                      │
                        │  • Decomposition → Telegram with Y/N     │
                        └──────┬─────────────────────────────────────┘
                               │ (you tap 👍 in Telegram)
                               ↓
                        ┌──────────────────────────────────────────┐
                        │  Within 15-90 min:                       │
                        │  • Lane workers dispatched in worktrees  │
                        │  • Each opens a PR with smoke evidence   │
                        │  • Telegram pings on each PR             │
                        └──────┬─────────────────────────────────────┘
                               │ (you tap merge in GH UI)
                               ↓
                        ┌──────────────────────────────────────────┐
                        │  Within ~3 min:                          │
                        │  • CI builds + publishes :latest         │
                        │  • Fleet auto-rolls all 6 tenants        │
                        │  • Telegram rollout summary              │
                        └──────────────────────────────────────────┘

Total Sir-time: ~30s to file + 1 tap to dispatch + 1 tap per merge.
Total Anthropic spend: $0/month (uses your Claude.ai subscription).
```

## What's in the package

| Layer | Tier | Files |
|---|---|---|
| **6 deterministic hooks** | 1 | `hooks/block-env-dump.sh`, `block-dead-ssh-aliases.sh`, `force-fetch-before-read.sh`, `enforce-worktree-isolation.sh`, `propose-memory-candidates.sh`, `require-smoke-evidence.sh` |
| **2 polish hooks** | 3 | `hooks/kill-criteria.sh`, `token-budget-warn.sh` |
| **11 slash commands** | 1+2+3 | `/lane-out`, `/lane-smoke`, `/cut-release`, `/fleet-pull`, `/cleanup-memory`, `/ultrareview` (Tier 1) + `/poll-and-act`, `/triage-issue`, `/setup-telegram` (Tier 2) + `/file-adr`, `/pm-dashboard` (Tier 3) |
| **4 subagent personas** | 1+2 | `agents/lane-worker.md`, `lane-orchestrator.md` (Tier 1) + `triage-bot.md`, `pr-reviewer.md` (Tier 2) |
| **5 smoke templates** | 2 | `smoke-templates/channel-token-route.md`, `workflow-trigger.md`, `migration-roundtrip.md`, `mcp-server-tool.md`, `wasp-op.md` |
| **3 GitHub Actions** | 2 | `workflows/notify-telegram.yml`, `pr-review-gate.yml`, `deploy-fleet.yml` |
| **1 desktop scheduled task** | 2 | `cron/alfred-code-poll.SKILL.md` |
| **5 docs** | all | `docs/smoke-as-truth.md`, `lane-protocol.md`, `setup-tutorial.md`, `autonomous-loop.md`, `operations-manual.md` |
| **ADR scaffolding** | 3 | `docs/decisions/` template + index |
| **Secrets broker** | 4 | `bin/secret`, `bin/secret-set`, `hooks/inject-secrets-registry.sh`, `commands/secrets-bootstrap.md`, `commands/secrets-status.md`, `docs/secrets-registry.md`, `docs/secrets-broker.md` |

## Install

```bash
# Tier 1 (this works on its own — hooks + commands + agents):
git clone https://github.com/ssdavidai/alfred-code ~/.claude/alfred-code
~/.claude/alfred-code/install.sh

# Tier 2 (Telegram + GH Actions + autonomous loop):
~/.claude/alfred-code/install-tier-2.sh

# Tier 3 (polish: kill criteria, token budget, ADRs, PM dashboard):
~/.claude/alfred-code/install-tier-3.sh

# Tier 4 (secrets broker — Claude stops asking for tokens):
~/.claude/alfred-code/install-tier-4.sh
```

Each tier is self-contained. Run only what you need. **Tier 1 alone delivers the worktree-isolation + smoke-gate + slash-command-vocabulary wins**, before you wire any Telegram.

## Read first

- **[docs/setup-tutorial.md](docs/setup-tutorial.md)** — 30-minute walkthrough, zero to autonomous
- **[docs/autonomous-loop.md](docs/autonomous-loop.md)** — architecture of the loop
- **[docs/operations-manual.md](docs/operations-manual.md)** — troubleshooting when something breaks
- **[docs/smoke-as-truth.md](docs/smoke-as-truth.md)** — why functional smoke gates everything
- **[docs/lane-protocol.md](docs/lane-protocol.md)** — how multi-PR issues decompose

## Why this exists

Sir was shipping `#120 Multi-Profile Hermes` in 7 lanes across 3 hours and 22 PRs. Half the friction was retyping the same boilerplate in every subagent prompt. The other half was forgetting to add `isolation: "worktree"` until two agents collided on `migrate.ts`. This package codifies the lessons so the next 100 lanes go smoothly.

The autonomous loop on top of it means Sir's not even the orchestrator anymore — he's the architect. He files an issue, taps Y, taps merge. The agents do the rest.

## Status

- **v0.1.0** — Tier 1 (hooks + commands + agents)
- **v0.2.0** — Tier 2 (Telegram channel + GH Actions + autonomous loop)
- **v0.3.0** — Tier 3 (kill criteria + token budget + ADRs + PM dashboard)
- **v0.4.0** — Tier 4 (secrets broker — Keychain-backed `secret get/set/list`)

All four tiers shipped 2026-05-31.

## Costs

| Component | Monthly cost |
|---|---|
| Claude on your Mac (interactive + scheduled task) | $0 — uses your existing Claude.ai sub |
| GitHub Actions runs | $0 — under free tier @ moderate volume |
| Telegram bot | $0 — Telegram is free |
| Fleet SSH deployments | $0 — your own VMs |
| **Total** | **$0** |

## When you'd want to pay for API tokens

- You want the autonomous loop running **while your Mac is asleep** → swap the Desktop scheduled task for an Anthropic Cloud Routine (~$5-30/mo at moderate volume)
- You want `claude-code-action` to auto-review every PR on GitHub's runners → API key + ~$5-20/mo

Neither is required. The free tier works.

## License

MIT. Take it, fork it, customize it for your own monorepo.

## Sources

The patterns in this package are distilled from:

- [How Boris Uses Claude Code](https://howborisusesclaudecode.com/) — Boris Cherny's daily habits with Claude Code
- [The Code Agent Orchestra](https://addyosmani.com/blog/code-agent-orchestra/) — Addy Osmani on multi-agent orchestration
- [Claude Code Channels](https://code.claude.com/docs/en/channels) — the Telegram/Discord/iMessage channel plugin system
- [Claude Code Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks) — the always-on heartbeat primitive
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) — the inherited-persona pattern
- The lived experience of shipping issue #120 in 7 lanes on 2026-05-30/31
