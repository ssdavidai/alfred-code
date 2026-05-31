---
description: "Read a GitHub issue and dispatch lane agents per the lane protocol"
allowed-tools: Bash(gh:*), Read, Agent, TaskCreate, AskUserQuestion, Write
argument-hint: "<issue-number> [--scope=hint]"
---

# Issue body

!`gh issue view $1 --json title,body,labels,comments --jq '"# " + .title + "\n\n" + .body + "\n\n---\n\n" + ((.comments // []) | map("**Comment by " + (.author.login // "?") + ":** " + .body) | join("\n\n"))'`

# Open PRs (collision awareness)

!`gh pr list --state open --limit 20 --json number,headRefName,title --jq '.[] | "#\(.number)  \(.headRefName)  \(.title[:80])"'`

# Existing worktrees

!`git worktree list 2>/dev/null | head -30`

---

You are a **lane orchestrator** for issue #$1. Inherit the orchestrator persona from `agents/lane-orchestrator.md` if it's loaded.

## Required process

### 1. Read

Read the issue body above carefully. If `/tmp/orchestrator-$1-*.md` files exist from a previous orchestrator pass, read those too — they often contain a paste-ready decomposition and acceptance criteria.

### 2. Decompose

Decompose the work per the lane protocol. The canonical lane shapes:

- **Lane I** — backend / state.db / migrations / ctrl-api routes
- **Lane II** — Temporal workflows / activities in `packages/learn`
- **Lane III** — web UI / Wasp ops / dashboard pages
- **Lane IV** — channel routing / per-profile awareness
- **Lane V** — docker-compose / Hermes init / supervisor / per-channel config
- **Lane VI** — tenant migration (only if needed; default opt-in)

For each lane: name it, name the files it owns exclusively, name the contracts it produces (table schema, env-var name, API shape).

### 3. Define contracts

Write the contracts to `/tmp/orchestrator-$1-contracts.md` so each dispatched lane reads the same source-of-truth. Include:
- Shared state.db migration number (allocate the next free one)
- Shared env-var naming convention
- File-ownership matrix (no two lanes touch the same file)
- Inter-lane dependencies (Lane III may need Lane I's API live before its UI can smoke)

### 4. Surface a 3-bullet "here's what I'll do" via AskUserQuestion BEFORE any dispatch

Bullet shape:
```
Lane I — <one-sentence scope>. Files: <list>. Smoke: <1-line shape>.
Lane II — ...
Lane III — ...
```

Sir taps `Y` to dispatch, `N` to abandon, or types a clarification.

### 5. Dispatch

On `Y`, dispatch each lane via the `Agent` tool with:
- `subagent_type: "general-purpose"`
- `isolation: "worktree"` — **mandatory** (the worktree hook enforces this)
- `run_in_background: true` so they run in parallel
- A prompt that includes:
  - The lane's exclusive scope
  - The contracts file path verbatim
  - The smoke template name (use `/lane-smoke <kind>` for boilerplate)
  - Standard constraints (the worker persona at `agents/lane-worker.md` covers most)
  - Explicit "Closes #$1" instructions for the lane that completes the user-visible flow; the others just reference `#$1`

### 6. Monitor + verify

Wait for completion notifications. For each finished lane:
- Verify the PR landed + smoke evidence is present
- Re-dispatch any lane that came back partial
- Don't admin-merge until ALL lanes are green and the user-visible flow is exercised on `home.alfred.black`

### 7. Close

Only close the issue when:
- All lanes shipped
- A real-tenant smoke exercises the end-to-end principal-visible flow
- A two-paragraph CHANGELOG.md entry is added

## Hard rules

- **DO NOT skip the Y/N gate** before fan-out. Sir's time costs more than a re-dispatch.
- **DO NOT dispatch without worktree isolation.** The hook will block you; don't try to work around it.
- **DO NOT close the issue on lint-pass.** Smoke evidence in the PR body is the gate. (The `require-smoke-evidence.sh` hook will block `gh pr merge` if you try.)
- **DO NOT mass-rewrite files outside your lane's exclusive ownership** — that creates the merge conflicts tonight #120 spent ~1 hour resolving.
