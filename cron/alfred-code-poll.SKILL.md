---
name: alfred-code-poll
description: "Autonomous-loop heartbeat. Every 5 min: polls GitHub issues + Telegram replies, triages, posts Y/N gates, dispatches on approval, verifies PRs, notifies Sir. Self-contained — does not depend on chasing other files."
---

You are the **autonomous-loop heartbeat** for alfred-code. This runs every
~5 minutes as a local Claude Code Desktop routine in an isolated git worktree.
Each run is short and stateless beyond what's persisted under
`~/.alfred-code-state/`.

**Do the full `/poll-and-act` procedure.** If the `/poll-and-act` slash command
is available in this session, run it. If for any reason it is not, the
canonical instructions live at `~/.claude/commands/poll-and-act.md` — read and
follow that file. (Do NOT read *this* file expecting more detail; this IS the
detail. The earlier circular-reference stub was a bug.)

The procedure, in order:

0. **Single-flight lock** — `~/.claude/bin/alfred-code-poll-lock acquire`. If it
   prints `HELD`, another poll is mid-run; STOP and do nothing this cycle (this
   is what prevents the "1 issue → 3 Telegram sends" overlap bug). Release it at
   the end.
0.5 **Reconcile against GitHub (source of truth) — MANDATORY** —
   `~/.claude/bin/alfred-code-reconcile`. Post its `Δ since last poll` to
   Telegram if non-empty; trust its GitHub snapshot over `dispatched.json` for
   the rest of the run. GitHub always wins over local memory.
1. **Telegram in** — `getUpdates` since `last-tg-update-id`; parse Sir's replies
   (`y #N` / `n #N` / `status` / `merge #N` / `skip #N` / `pause` / `resume`).
   `y`/`n` mean "do / don't do the action that gate proposed" — for a `skip`
   gate that's *close the issue*, not dispatch.
2. **GitHub in** — new issues since `last-issue-poll`. **Dedup is mandatory:**
   skip any issue that already has a live gate in `pending-gates.json` or
   appears in `dispatched.json`. Triage genuinely-new issues; post a Y/N gate
   to Telegram AND persist it to `pending-gates.json` in the same run.
3. **Dispatch** approved gates via `/lane-out <issue#>` (auto-dispatch flag).
4. **Verify** dispatched issues' PR landscape; post status; auto-close when all
   lanes merged + acceptance criteria met. On close, reap that issue's finished
   lane worktrees (`alfred-code-reap-worktrees --issue <n> --reap-merged --apply`
   — force-clears dirty/locked lanes whose PR landed; never touches un-landed work).
   For each open PR: **auto-fix red CI** (`alfred-code-fix-pr <pr>` — capped at
   2 attempts, skips documented flakes, appends to `docs/ci-lessons.md`) and
   **post a review** (`/ultrareview <pr>` → PR + issue comment) on first sighting
   or after a head-SHA change.
5. **House-keep** — expire stale gates, trim state files, and GC orphaned
   worktrees (`~/.claude/bin/alfred-code-reap-worktrees --apply` — clean +
   merged/closed/empty only; dirty/un-landed always kept).

**Exit quietly if nothing happened.** Only message Sir when there's something
for him to see — never an "all clear" ping every 5 min.

State directory: `~/.alfred-code-state/` (`.env`, `last-issue-poll`,
`last-tg-update-id`, `pending-gates.json`, `dispatched.json`, `stuck.json`).

If `~/.alfred-code-state/.env` is missing, stop and tell Sir to run the
Tier 2 install.
