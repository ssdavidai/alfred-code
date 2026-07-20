# Alfred Code v2 control plane

This is the executable specification for GitHub issues #5 through #11 in `ssdavidai/alfred-code`. The implementation lives in `src/alfred_code`; this document explains the invariants, but it is not an authority. The authoritative evidence is the source, SQLite event log, append-only JSONL audit log, live GitHub objects, live Superset objects, git objects, and CI output.

## Outcome

GitHub is the product-management and decision surface. With `github.auto_intake = true`, every open issue is intake; label-gated intake remains available when it is false. The controller immediately projects an enrolled issue as `Specifying`, inspects the live repository and GitHub state, generates a lane-aware plan pinned to the current default-branch SHA, validates it deterministically, and comments it on the issue. The operator approves exactly that plan with `/approve-plan <full-plan-sha256>`, rejects it with `/reject-plan <full-plan-sha256>`, or leaves specification feedback as a normal comment. Superset creates and owns the isolated workspaces and starts the configured worker/reviewer runtime. GitHub PRs and checks are the delivery authority. Slack is optional notification output only.

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

Every plan contains the GitHub issue number, exact default-branch SHA, hash of the issue body, hash of the issue body plus accepted operator feedback, summary, risk, and one job per affected lane. Each job declares a stable ID, canonical lane branch, bounded paths, real verification command, contracts read, contracts changed, dependencies, and observable acceptance evidence.

The normalizer computes SHA-256 over canonical JSON. Approval includes the full hash. Editing the issue or regenerating the plan changes the hash. Advancing the default branch before approval invalidates the plan. Once execution starts, an issue body edit blocks new execution rather than silently changing active scope.

The planner is pinned to Codex `gpt-5.6-sol` with high reasoning, an ephemeral session, disabled web search, strict config validation, and the dedicated `alfred-planner` permission profile. MCP servers, plugins, and memories are replaced with empty or disabled configuration. The profile grants read access only to the current workspace and minimal toolchain paths, denies credential-shaped workspace files, grants no workspace or home-directory writes, filters secret-bearing environment variables, and disables network access. The planner receives controller-collected evidence over stdin and may inspect but cannot modify the checkout. Its final response is constrained by the plan JSON Schema before the deterministic validator rejects unknown or repeated lanes, non-global job IDs, wrong branch prefixes, forbidden-zone writes outside phase0, writes outside the lane allowlist, overlapping path ownership, empty verification, missing dependencies, cycles, contract changes outside phase0, and downstream jobs that fail to depend on a phase0 contract job.

## Job lifecycle

```text
queued
  -> waiting_dependency -> queued
  -> waiting_lane       -> queued
  -> launching          -> running
  -> running            -> pr_open
  -> pr_open            -> reviewing
  -> reviewing          -> repairing -> pr_open
  -> reviewing          -> ready_merge
  -> ready_merge        -> merged

Any active state -> blocked when live evidence fails
Open PR + closed issue -> quarantined
Closed unmerged PR -> quarantined
No PR + closed issue -> closed
```

The scheduler first looks for a PR by the canonical branch. This lets it adopt real progress after a restart instead of relaunching. If there is no PR, it verifies dependencies, atomically acquires the lane lease in SQLite, writes a `launching` intent, creates a branch from the approved base commit without moving any existing ref, and asks Superset to create the workspace and start the worker in one command.

Workspace names are deterministic. A crash after Superset accepts a create can be reconciled by name. A conflicting pre-existing name or branch blocks rather than being overwritten. The same approach is used for review workspaces, which include PR number and head-SHA prefix.

Workspace creation is not agent liveness. The scoped launcher must publish an atomic `.alfred-code-launch.json` handshake from a supported Python runtime, then replace it with a completed, exited, or failed status when the provider returns. A result marker is not final while the provider is still running. The controller blocks promptly on a real exit or a missing handshake and preserves the workspace. A distinct progress timeout applies only after a live launch, so lifecycle state cannot remain `running` merely because Superset still has a workspace record.

Codex workers use an ephemeral named permission profile with approvals disabled, network disabled, integrations removed, secret-bearing environment variables filtered, and repository writes limited to the manifest paths, ignored verification outputs, and the role's result marker. The launcher obtains the exact current hash of its non-managed `PreToolUse` lane guard from the local Codex app server and writes the matching trust record into that profile. It never passes a hook-trust bypass. A missing probe, untrusted hook, legacy sandbox override, or unavailable guard fails launch before the provider starts. Read-only grants cover only trusted toolchains, Git worktree metadata, and validated dependency targets. If a tracked package dependency link is broken, the launcher may create a new root `node_modules` resolution overlay to a compatible same-origin checkout; it never replaces the tracked link or any existing path.

On a launcher-policy revision, the controller may retry a completed old-policy blocker in the same workspace after it has independently proved that every existing change remains in scope and no file was deleted. Runtime markers and the dependency overlay are excluded from source-diff evidence; other unplanned paths still quarantine the job. Workers never receive Git write access. After a ready result, the trusted controller reruns verification and stages only the exact approved files. The phase0 controller commit uses `--no-verify` only because the repository hook rejects phase0 commits from linked worktrees; manifest validation, scope validation, deletion rejection, and verification have already succeeded before that controller-only command.

Before review, the controller fetches the complete PR file list from GitHub and compares every filename to the approved plan paths. An extra file blocks regardless of worker claims or hooks. Green CI is not enough unless the PR body also contains its smoke-evidence section.

Review passes only when GitHub CI is green and an allow-listed independent reviewer posts `<!-- alfred-code-review:<exact-head-sha>:pass -->` after the controller requested review. A marker posted before the independent review intent, by another actor, or for another SHA is ignored. A new commit changes HEAD, invalidates the old marker automatically, and produces a distinct review workspace. A passing review makes the job `ready_merge`; the operator still merges in GitHub.

A failing review marker starts a bounded repair cycle in the original worker workspace. The controller preserves the approved plan and lane, proves that the worktree is clean at the exact failed PR SHA, writes a one-attempt `.lane` handoff, and launches only the scoped worker preset. The repair agent remains offline, cannot write Git metadata, and can edit only the already-approved paths. Its result must echo the exact failed SHA, attempt number, and controller nonce; stale result files cannot complete the handoff. The trusted controller then rejects deletions and scope drift, reruns verification, commits and pushes the repair, and waits for CI plus a new independent review workspace at the new SHA. `review_repair_max_attempts` defaults to two; exhausting it leaves the PR blocked for operator attention instead of looping forever. Red CI still blocks separately.

## Issue lifecycle

The issue state is derived from current plan and job state. `planning` means an automatically enrolled issue is being specified. `awaiting_approval` means a current immutable plan exists with no valid decision. A normal allow-listed operator comment after that plan invalidates it and supplies context to a fresh plan. An exact rejection moves the issue to `blocked` without materializing jobs. `building` means at least one approved job is active, including an exact-SHA repair. `ready_merge` means every job is either ready or merged. `completed` means every planned job is merged. `blocked` also covers jobs that are blocked, quarantined, closed without merge, or have made no repository progress before the configured worker timeout. `closed` mirrors a closed GitHub issue after its PRs have been refreshed and classified.

GitHub Projects is a projection, not another state machine. The controller updates `Control stage`, `Risk`, `Plan hash`, `Lane set`, and `Runtime`. Project metadata and items are loaded once per daemon process and then updated from controller-owned transitions, avoiding an expensive full GraphQL refresh on every poll. If project sync fails, execution can continue because the durable event records the visibility failure. Slack behaves the same way: notification failure never mutates delivery truth.

## Persistence and restart behavior

SQLite uses WAL, foreign keys, a 30-second busy timeout, and `BEGIN IMMEDIATE` for transitions and leases. Plans and events are immutable. Approvals are revocable only by superseding the plan. Notifications have stable dedupe keys and retry counters. Legacy JSON is imported into `observations` and `legacy_imports` as evidence only; a legacy string saying `building` can never materialize a v2 job.

The controller owns a non-blocking file lock while serving, so two launchd processes cannot schedule simultaneously. External effects use deterministic identifiers and intent-before-effect state. A restart re-reads GitHub and Superset before taking the next action.

## Safe default

`apply = false` is the shipped default. In this mode a cycle reads GitHub and updates observations but does not invoke a planner, comment, create a branch, create a workspace, or start an agent. `doctor` must validate the repo, lane policy, database, GitHub credentials, Superset authentication, and optionally the GitHub Project before `apply` should be enabled.
