# The lane protocol

Decompose a multi-PR issue into independent lanes with explicit contracts. Each lane is one PR's worth of work; lanes don't share files; the user-visible flow is the close gate.

## The lane shapes

| Lane | Owns | Typical files |
|---|---|---|
| **I** — backend | state.db migrations + ctrl-api routes + shared TS libs | `packages/ctrl/src/db/migrations/NNNN_*.sql`, `packages/ctrl/src/api/routes/<thing>.ts`, `packages/ctrl/src/db/<thing>.ts`, route tests |
| **II** — workflows | Temporal workflows + activities in `packages/learn` | `packages/learn/src/workflows/<thing>.py`, `src/activities/<thing>.py`, `tests/test_<thing>.py` |
| **III** — UI | Wasp pages + ops + components | `packages/web/main.wasp`, `src/dashboard/<Page>.tsx`, `src/dashboard/operations.ts` |
| **IV** — channel routing | Per-profile / per-channel resolution + adapter logic | `packages/ctrl/src/api/routes/channels_*.ts`, `alfredDeliver.ts`, channel-token store |
| **V** — compose / infra | docker-compose + Hermes init + supervisor + env templates | `docker-compose.yaml`, `packages/hermes/init/`, `packages/hermes/docker/`, `*.njk` |
| **VI** — tenant migration | Opt-in tenant data conversions (rare) | one-shot scripts + an ADR |

Three lanes is common. Six is rare.

## The contracts file

For every multi-lane issue, write `/tmp/orchestrator-<issue>-contracts.md` before dispatching:

```markdown
# Contracts for #<n>

## Migration
- Next free: 0NNN
- user_version after: NN → NN+1

## Env-var convention
- Per-profile writes: resolveProfileEnvPath(slug)
- Per-channel keys: <UPPERCASE_KIND>_<FIELD>

## File-ownership matrix
| Lane | Exclusive files |
|---|---|
| I | packages/ctrl/src/db/migrations/0NNN_*.sql, src/db/X.ts, src/api/routes/X.ts |
| II | packages/learn/src/workflows/Y.py, src/activities/Y.py |
| III | packages/web/src/dashboard/ZPage.tsx, operations.ts (additions only — append, don't rewrite) |

## Inter-lane deps
- Lane III's UI smoke gated on Lane I's route being live (deploy Lane I first)

## Cascade rules
- When Lane II archives a row, Lane IV's bound bindings: pick one of:
  (i) cascade unbind on archive — Lane I owns this
  (ii) resolver-side fallback — Lane IV owns this
  Pick: (i)
```

Each lane reads this verbatim. Contract changes must come back to the orchestrator.

## The Y/N gate (mandatory)

Before any `Agent` dispatch:

```
AskUserQuestion({
  question: "Lane decomposition for #<n>: proceed?",
  options: ["Yes — dispatch", "Wait — refine", "Skip — leave open"],
  header: "#<n> dispatch",
})
```

Each lane's bullet in the question body:

```
Lane I — agent_profile registry + CRUD routes
  Files: 0017_agent_profiles.sql, src/db/agentProfiles.ts, src/api/routes/profiles.ts
  Smoke: insert 1 profile + list + bind channel + archive + assert main unaffected
```

## Dispatch shape

```python
Agent({
  subagent_type: "general-purpose",
  isolation: "worktree",                # MANDATORY — hook blocks otherwise
  run_in_background: true,              # for parallel lanes
  description: "Lane <N> — <one-liner>",
  prompt: f"""
Inherit lane-worker.md. Your lane scope:
  {lane_scope}

Contracts at /tmp/orchestrator-{n}-contracts.md (read verbatim, don't drift).

Standard smoke template:
  {get_smoke_template_for(kind)}

Open PR titled `feat(...): #{n} Lane <N> — <one-liner>`. PR body:
  - References `#{n}` (not Closes — only the lane that completes the flow uses Closes)
  - Includes `## Smoke evidence` section (mandatory; the hook will block merge otherwise)
"""
})
```

## Verification per lane

After completion:

1. Read the PR diff. Did it stay in scope?
2. Read the `## Smoke evidence`. Real, not theater? (Isolation assertion present?)
3. If anything's wrong: re-dispatch with the gap named in the new prompt. Don't try to fix it in cargo-cult iterations.

## Closing the issue

Only when:

- ✓ All lanes' PRs are merged
- ✓ Real-tenant smoke exercises the principal-visible end-to-end flow on `home.alfred.black`
- ✓ Two-paragraph CHANGELOG.md entry under a new `[YYYY-MM-DD]` heading

Until then: leave open, post a comment with the queue.

## Sir-8 / Wave protocol references

This protocol is descended from Sir's earlier ad-hoc patterns:
- **Sir-8 Lanes I/II/III/V** — first formalized in the 2026-05 multi-lane runs
- **Wave A/B/C/D/E** — the staged-deploy protocol for issues #109-#114, with each Wave being a coordinated round of agent dispatches across multiple issues

The lane protocol generalises both into one shape: decompose, contract, gate, dispatch, verify, close.
