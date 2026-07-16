# alfred-code on Paperclip + Hermes — DRAFT design (not deployed)

Port the Telegram/laptop harness to run 24/7 on home.alfred.black using ONLY
configuration: Hermes cron (verified: `cron/scheduler.py` + `curl /v1/responses`
fires an agent turn), Paperclip as control plane + state + gates, codex-builder
as the hands, Vaultwarden as the keyring, the Desk as the human mirror.

Nothing here is wired yet. This is the skill + the idempotency contract for review.

---

## 0. Topology (config-only)

| Piece | Where | Config action |
|---|---|---|
| Clock | Hermes `cron.jobs[]` on the **workers** profile, `*/5 * * * *`, `overlap=skip` | edit `config.yaml` + restart |
| Foreman turn | cron `command` = `curl -s -XPOST 127.0.0.1:18790/v1/responses -H "Authorization: Bearer $API_SERVER_KEY" -H "X-Hermes-Session-Key: alfred-code-foreman" -d '{"input":"Run the alfred-code-foreman skill."}'` | in the cron command string |
| Foreman logic | `alfred-code-foreman` **skill** (markdown) in the Hermes workspace | drop skill file |
| Work intake | poll GitHub via Composio github MCP (later: GH Action push) | Composio github connection |
| Control plane | Paperclip issues + approvals + comments (the state + gates + audit) | via paperclip MCP tools |
| Builder | assign Paperclip issue → `codex-feature-builder` (:18793) | native, already wired |
| Keyring | Vaultwarden MCP (foreman reads GH PAT); codex-builder uses sealed deploy key | native |
| Human mirror | each Paperclip approval also writes a `needs_attention`/decision via `alfred` MCP | foreman double-write |
| Notify | `schedule_prompt(channel=telegram)` / `notify_principal` | native |
| Smoke gate | `pr-review-gate.yml` required check stays on GitHub | already exists |

---

## 1. State machine (one per GitHub issue ↔ Paperclip issue pair)

The foreman is a **stateless reconciler**: each tick it re-derives every item's
state from live GitHub + Paperclip + markers, then fires **at most one** action
per item. Every action writes the marker that suppresses its own re-fire, so an
unchanged world is a no-op.

```
NEW ──spec──▶ SPEC_PENDING ──(human approves)──▶ APPROVED ──assign──▶ BUILDING
  └ research+spec, GH comment,        │ wait                 │ wait (heartbeat→codex)
    create Paperclip issue+approval    │                      ▼
                                       │                  PR_OPEN ──review──▶ REVIEWED
                                       │                                        │
                          (human rejects → REJECTED, close)                     ▼
                                                              ┌── CI red / review reject ──┐
                                                              │  attempts<cap → dispatch    │
                                                              │  fix → BUILDING (attempt++) │
                                                              └─────────────────────────────┘
                                                              │  green + pass
                                                              ▼
                                                    MERGE_PENDING ──(human approves)──▶ MERGE_APPROVED
                                                              │ wait                         │ merge PR
                                                              ▼                               ▼
                                                       (attempts≥cap → HELD, notify)        DONE (close issues)
```

| State | Detected by | Action (≤1) |
|---|---|---|
| NEW | open GH issue with no Paperclip mirror | research+spec → GH comment (spec marker) → create Paperclip issue + build-approval; mirror map |
| SPEC_PENDING | mirror exists, build-approval open | none (human) |
| APPROVED | build-approval = approved, issue not yet assigned | assign issue → `codex-feature-builder`; status→in_progress |
| BUILDING | assigned, no PR yet OR open PR with unseen head SHA pending build | none (heartbeat drives codex) |
| PR_OPEN | PR exists, no review marker for current head SHA | `/ultrareview`-equiv turn → post PR review + review marker(sha) |
| REVIEWED | review marker matches head SHA | evaluate: green+pass → create merge-approval (+ Desk card); red/reject & attempts<cap → dispatch fix (attempt++); attempts≥cap → HELD + notify |
| MERGE_PENDING | merge-approval open | none (human) |
| MERGE_APPROVED | merge-approval = approved | merge PR → close Paperclip issue + GH issue → notify |
| HELD / REJECTED / DONE | terminal markers | none |

---

## 2. State-marker conventions (the idempotency contract)

**No JSON state files.** State lives in GitHub + Paperclip, which are durable and
authoritative. Markers are content-addressed so re-runs are safe.

| Question the tick asks | Marker / source of truth | Keying |
|---|---|---|
| Is this GH issue already mirrored? | Paperclip issue whose body/field holds `gh_ref: ssdavidai/alfred#<n>` (search before create) | per GH issue number |
| Did I post the spec? | GH issue comment containing `<!-- alfred-code:spec v1 -->` | one per issue |
| Approved to build? | Paperclip **build-approval** status (`approved`/`rejected`/`pending`) | the approval object |
| Already dispatched to builder? | Paperclip issue `assignee == codex-feature-builder` AND status `in_progress` | issue fields |
| Did I review this PR at its current commit? | PR comment containing `<!-- alfred-code:review sha=<HEAD_SHA> verdict=<pass\|hold> -->` | **keyed by head SHA** |
| How many fix attempts? | Paperclip issue field `fix_attempts: <int>` (or count of `<!-- alfred-code:fix -->` comments) | per issue; cap = 3 |
| Merge approved? | Paperclip **merge-approval** status | the approval object |
| Already merged / closed? | GH PR `merged==true` / GH issue `state==closed` | GitHub |

**The load-bearing rule:** *an action is only taken when its marker is absent;
taking it creates the marker.* The SHA-keyed review marker is what lets a
codex-builder fix (new commit → new SHA) correctly re-trigger review while an
unchanged PR is never re-reviewed — identical to alfred-code's
"review on first sighting or head-SHA change."

**Single-flight:** the cron job runs `overlap=skip` (verified native; same
mechanism that serialised today's reflection runs) so two ticks can't both see
NEW and double-create.

---

## 3. The skill — `alfred-code-foreman/SKILL.md` (draft)

```markdown
---
name: alfred-code-foreman
description: Autonomous engineering foreman. Each invocation reconciles GitHub
  (ssdavidai/alfred) against Paperclip, advancing every issue through its state
  machine by AT MOST ONE action, idempotently. GitHub + Paperclip are the only
  state — never assume, always re-derive from markers.
---

You are the **alfred-code foreman**. You run on a 5-minute cron tick. Each run is
short, stateless, and must be SAFE TO REPEAT: if nothing changed since the last
tick, you do nothing. You never take an action whose marker already exists.

## Credentials
- GitHub: fetch the PAT from Vaultwarden (`vaultwarden` MCP, item `alfred-code-github-pat`)
  at the start of the run. Use `gh`/Composio github with it. Never print it.
- Paperclip: you act as the foreman agent (your own session).
- codex-feature-builder pushes with its own sealed deploy key — you never hand it creds.

## Procedure (run top to bottom, then stop)

0. RECONCILE — pull the live world, trust it over any memory:
   - GitHub: open issues, open PRs (+ head SHA, mergeable, CI rollup), recently merged.
   - Paperclip: your issues, their statuses, open approvals + decisions.

1. For each OPEN GitHub issue with NO Paperclip mirror (`gh_ref` search is empty):
   - Do the research + spec: read the issue + relevant code; produce LANES +
     CONTRACTS + acceptance criteria + smoke plan.
   - Post it as a GitHub comment ending with `<!-- alfred-code:spec v1 -->`.
   - Create a Paperclip issue (body carries `gh_ref: <owner/repo#n>` + the spec).
   - Raise a Paperclip **build-approval** AND mirror it as a Desk decision card
     (`alfred` MCP). Then STOP on this item (await human).

2. For each Paperclip issue whose build-approval is APPROVED and not yet assigned:
   - Assign the issue to `codex-feature-builder`, set status in_progress.
     (The heartbeat dispatches it; it builds in /work, pushes a branch, opens a PR.)

3. For each OPEN PR with no `<!-- alfred-code:review sha=<HEAD_SHA> ... -->` comment
   for its CURRENT head SHA:
   - Run an ultrareview pass (read the diff, the linked spec/contracts, CI).
   - Post the review as a PR comment ending with
     `<!-- alfred-code:review sha=<HEAD_SHA> verdict=<pass|hold> -->`.

4. For each REVIEWED PR (review marker matches head SHA):
   - GREEN CI + verdict=pass → raise a Paperclip **merge-approval** + Desk card. STOP (human).
   - RED CI or verdict=hold:
       - read `fix_attempts`; if < 3 → bump it, dispatch codex-feature-builder
         to fix WITH the failure context (CI log / review). Back to BUILDING.
       - if ≥ 3 → set issue HELD, notify Sir (telegram), STOP.

5. For each merge-approval that is APPROVED:
   - Merge the PR (squash). Close the Paperclip issue + the GH issue. Notify Sir.
   - (Merge → existing build-* + deploy-fleet rolls it to the fleet automatically.)

6. NOTIFY — if anything changed this tick (new spec, new PR, new gate, a merge,
   a HELD), send Sir a one-line delta via telegram. If nothing changed, stay silent.

## Hard rules
- At most ONE state-advancing action per item per tick.
- Never act if the suppressing marker is present.
- Merge is HUMAN-ONLY: you may merge ONLY after a merge-approval is approved.
- Fix loop is capped at 3 attempts per PR; then HELD + notify.
- Smoke evidence: the PR must carry a `## Smoke evidence` section or
  `pr-review-gate.yml` blocks the merge — surface that in your review if missing.
```

---

## 4. Open decisions for Sir
1. **Scheduler:** Hermes cron (verified) vs a Paperclip Routine. Hermes cron is
   proven; Routine keeps it one-platform. Lean: Hermes cron to start.
2. **GitHub creds:** Vaultwarden item `alfred-code-github-pat` (scoped: repo +
   PR + merge) vs a Composio GitHub OAuth connection. Lean: PAT in Vaultwarden
   (simplest, matches "creds live in VW").
3. **Token budget:** each tick ≈ 40k input tokens on workers. 5-min ticks = ~12/hr.
   Consider a leaner foreman profile or a longer interval (e.g. 10 min) for cost.
4. **Scope v1:** ship triage→spec→gate→dispatch first; add review→fix→merge once
   the front half is proven. Or all at once.
```
