---
description: "Multi-agent review of a PR before merge. Reads diff + lane contract + smoke evidence."
allowed-tools: Bash(gh:*), Read, Agent, AskUserQuestion
argument-hint: "<pr-number>"
---

# PR snapshot

!`gh pr view $1 --json number,title,headRefName,baseRefName,additions,deletions,changedFiles,state,body --jq '.'`

# Diff stat

!`gh pr diff $1 --name-only 2>/dev/null | head -40`

# CI status

!`gh pr checks $1 2>&1 | head -20`

---

You are the **review orchestrator** for PR #$1. Spawn three focused subagents in parallel and synthesize their findings into a single ship/hold recommendation.

## The three reviewers (dispatch in parallel)

### Reviewer A — Contract reviewer
- Inherits `lane-worker.md` persona
- Reads the lane decomposition for this PR's issue (look in the PR body or `/tmp/orchestrator-*.md`)
- Verifies the PR honors its exclusive file-ownership and doesn't drift the contract
- Reports: contract honored ✓ / broken ✗ (with what broke)

### Reviewer B — Smoke evidence reviewer
- Reads the PR body's `## Smoke evidence` block
- Checks each step actually exercised the design intent (not just "200 OK")
- Specifically verifies: isolation assertion present + audit-row presence + cleanup
- Reports: smoke is real ✓ / theater ✗ (with what's missing)

### Reviewer C — Code reviewer
- Reads the full diff
- Looks for the recurring traps Sir's memory captures:
  - Wasp `Promise<T>` trap (must be `Promise<any>`)
  - Stale alfred-platform paths (must use /Users/ssd/dev/alfred)
  - Bare env dumps in test scripts
  - Hardcoded `main` profile where multi-profile resolution should fire
  - SQLite `immutable=1` flag (catches stale WAL rows)
  - Worktree-collision-shaped edits (multiple lanes touching shared files)
- Reports: clean ✓ / found issues ✗ (with file:line)

## After all three return

Synthesize a single recommendation. Three buckets:

- **SHIP** — all three green. Run `gh pr merge $1 --squash`. The smoke-gate hook will check the body has the right section.
- **HOLD** — at least one reviewer flagged something fixable. Post a PR comment summarising what needs to change, do not merge.
- **REJECT** — fundamental contract break or smoke theater. Post a PR comment explaining, recommend the lane be re-dispatched.

### Post the verdict in BOTH places (always)

1. **On the PR** (`gh pr comment $1`) — the per-PR verdict (contract/smoke/code + the one most important thing).
2. **On the parent issue** — Sir tracks the *feature* on the issue, not the PRs. Derive the parent issue number from the PR body (look for `Closes #N` / `#N`), then `gh issue comment <N>` with a consolidated table row for this PR's verdict + the cross-PR merge order + any post-merge live-test gate. If a consolidated comment for this issue already exists this run, append to your synthesis rather than duplicating. This is mandatory — a review that only lands on the PR is invisible to the issue's reader.

Use `AskUserQuestion` to surface the recommendation to Sir as a Y/N gate before any merge:
```
"PR #$1 review complete: SHIP / HOLD / REJECT (<one-line reason>). Proceed?"
```

## Honesty rule

If the three reviewers disagree (one says SHIP, another says REJECT), **side with the most cautious** by default. Sir can override; you cannot un-merge.
