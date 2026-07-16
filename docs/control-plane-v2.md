# Alfred Code v2 control plane

This is the executable specification for GitHub issues #5 through #11 in `ssdavidai/alfred-code`. The implementation lives in `src/alfred_code`; this document explains the invariants, but it is not an authority. The authoritative evidence is the source, SQLite event log, append-only JSONL audit log, live GitHub objects, live Superset objects, git objects, and CI output.

## Outcome

GitHub is the product-management and decision surface. A labeled issue is intake. The controller inspects the live repository and GitHub state, generates a lane-aware plan pinned to the current default-branch SHA, validates it deterministically, and comments it on the issue. The operator approves exactly that plan by commenting `/approve-plan <full-plan-sha256>`. Superset creates and owns the isolated workspaces and starts the configured worker/reviewer runtime. GitHub PRs and checks are the delivery authority. Slack is optional notification output only.

The controller never merges. It never closes an issue or PR. It never deletes a branch. It never force-pushes or resets a branch. Workspace deletion is separately configured, is off by default, and is eligible only after GitHub reports the associated PR as merged.

## Authority map

| Question | Authority | Local durable copy |
|---|---|---|
| What work exists? | GitHub issue | `issues` and `observations` |
| What source was specified? | Git commit object at `base_sha` | immutable `plans.plan_json` |
| What lane may write a path? | target repo `scripts/hooks/lanes.json` | plan validation result |
| Did the operator approve? | exact GitHub comment by an allow-listed login | `approvals` |
| Does an agent workspace exist? | Superset workspace API | job workspace fields and observation |
| Is delivery open/green/merged? | GitHub PR and check rollup | job PR fields and observation |
| Did independent review pass? | SHA-bound marker in a PR comment | `review_sha` |
| What did the controller believe and why? | SQLite events plus JSONL audit | both are append-only |

Local state is a cache and event ledger, never an excuse to skip refreshing an external authority. If GitHub or Superset cannot be refreshed, the controller does not advance that object.

## Plan contract

Every plan contains the GitHub issue number, exact default-branch SHA, hash of the issue body, summary, risk, and one job per affected lane. Each job declares a stable ID, canonical lane branch, bounded paths, real verification command, contracts read, contracts changed, dependencies, and observable acceptance evidence.

The normalizer computes SHA-256 over canonical JSON. Approval includes the full hash. Editing the issue or regenerating the plan changes the hash. Advancing the default branch before approval invalidates the plan. Once execution starts, an issue body edit blocks new execution rather than silently changing active scope.

The planner is run with Claude safe mode, plan permissions, no tools, no browser, no slash commands, and no session persistence. Its only input is controller-collected evidence over stdin. The validator rejects unknown or repeated lanes, non-global job IDs, wrong branch prefixes, forbidden-zone writes outside phase0, writes outside the lane allowlist, overlapping path ownership, empty verification, missing dependencies, cycles, contract changes outside phase0, and downstream jobs that fail to depend on a phase0 contract job.

## Job lifecycle

```text
queued
  -> waiting_dependency -> queued
  -> waiting_lane       -> queued
  -> launching          -> running
  -> running            -> pr_open
  -> pr_open            -> reviewing
  -> reviewing          -> ready_merge
  -> ready_merge        -> merged

Any active state -> blocked when live evidence fails
Open PR + closed issue -> quarantined
Closed unmerged PR -> quarantined
No PR + closed issue -> closed
```

The scheduler first looks for a PR by the canonical branch. This lets it adopt real progress after a restart instead of relaunching. If there is no PR, it verifies dependencies, atomically acquires the lane lease in SQLite, writes a `launching` intent, creates a branch from the approved base commit without moving any existing ref, and asks Superset to create the workspace and start the worker in one command.

Workspace names are deterministic. A crash after Superset accepts a create can be reconciled by name. A conflicting pre-existing name or branch blocks rather than being overwritten. The same approach is used for review workspaces, which include PR number and head-SHA prefix.

Before review, the controller fetches the complete PR file list from GitHub and compares every filename to the approved plan paths. An extra file blocks regardless of worker claims or hooks. Green CI is not enough unless the PR body also contains its smoke-evidence section.

Review passes only when GitHub CI is green and an allow-listed independent reviewer posts `<!-- alfred-code-review:<exact-head-sha>:pass -->` after the controller requested review. A marker posted before the independent review intent, by another actor, or for another SHA is ignored. A new commit changes HEAD, invalidates the old marker automatically, and produces a distinct review workspace. A failing marker or red CI blocks. A passing review makes the job `ready_merge`; the operator still merges in GitHub.

## Issue lifecycle

The issue state is derived from current job state. `awaiting_approval` means a current immutable plan exists with no valid approval. `building` means at least one approved job is active. `ready_merge` means every job is either ready or merged. `completed` means every planned job is merged. `blocked` means at least one job is blocked, quarantined, or closed without merge. `closed` mirrors a closed GitHub issue after its PRs have been refreshed and classified.

GitHub Projects is a projection, not another state machine. The controller updates `Control stage`, `Risk`, `Plan hash`, `Lane set`, and `Runtime`. If project sync fails, execution can continue because the durable event records the visibility failure. Slack behaves the same way: notification failure never mutates delivery truth.

## Persistence and restart behavior

SQLite uses WAL, foreign keys, a 30-second busy timeout, and `BEGIN IMMEDIATE` for transitions and leases. Plans and events are immutable. Approvals are revocable only by superseding the plan. Notifications have stable dedupe keys and retry counters. Legacy JSON is imported into `observations` and `legacy_imports` as evidence only; a legacy string saying `building` can never materialize a v2 job.

The controller owns a non-blocking file lock while serving, so two launchd processes cannot schedule simultaneously. External effects use deterministic identifiers and intent-before-effect state. A restart re-reads GitHub and Superset before taking the next action.

## Safe default

`apply = false` is the shipped default. In this mode a cycle reads GitHub and updates observations but does not invoke a planner, comment, create a branch, create a workspace, or start an agent. `doctor` must validate the repo, lane policy, database, GitHub credentials, Superset authentication, and optionally the GitHub Project before `apply` should be enabled.
