---
description: "Triage a single GitHub issue — categorize, decompose if needed, post Y/N gate to Telegram."
allowed-tools: Bash(gh:*, curl:*, jq:*), Read, Write, AskUserQuestion
argument-hint: "<issue-number>"
---

# Issue body

!`gh issue view $1 --json title,body,labels,comments,createdAt --jq '"# " + .title + "\n\n" + .body + "\n\nCreated: " + .createdAt'`

# Recent merged PRs (collision awareness)

!`gh pr list --state merged --limit 10 --json number,title,mergedAt --jq '.[] | "  #\(.number)  \(.title[:80])  merged \(.mergedAt)"'`

# Existing open issues with related labels

!`labels=$(gh issue view $1 --json labels --jq '.labels | map(.name) | join(",")') ; if [ -n "$labels" ]; then gh issue list --state open --label "$labels" --limit 10 --json number,title --jq '.[] | "  #\(.number)  \(.title[:80])"'; fi`

---

You are the **triage bot**. Inherit `agents/triage-bot.md` if loaded.

## Process

### 1. Classify

Pick one:

- **trivial-fix** — one-line change, no decomposition. Examples: typo, version bump, casing mismatch, dead URL, missing log line.
- **scoped-feature** — 1-3 lanes. Examples: new MCP tool, new ctrl-api route, new per-profile field.
- **epic** — 4+ lanes. Examples: multi-profile Hermes (#120), file storage system (#114), Tier-4 HA autonomy (#115).
- **research** — needs investigation before code. Examples: "should we use Vexa or Recall.ai?", "is per-profile email achievable on AgentMail?".
- **skip** — already covered, duplicate, out of scope, or already-fixed-but-not-closed.

Justify your pick in one sentence.

### 2. Decompose (only for scoped-feature + epic)

Per the lane protocol (`docs/lane-protocol.md`):

- Pick which lane shapes apply (I/II/III/IV/V/VI)
- For each lane: name the scope in one sentence + name 2-4 files it owns exclusively + name a smoke shape

For trivial-fix: name the single file + line + change.

### 3. Surface

If you're being invoked from `/poll-and-act` (background, no human in the loop), use the **persistence path**:
- Compose the Telegram message
- Write the gate to `~/.alfred-code-state/pending-gates.json`
- Send the Telegram message
- Return the gate ID

If you're being invoked interactively by Sir (e.g. he typed `/triage-issue 220` at the prompt), use the **AskUserQuestion path**:
- Surface the triage + decomposition
- Ask Y/N via `AskUserQuestion`
- On Y: dispatch via `/lane-out $1`

## Decomposition output shape

```
Triage: <category>
Reason: <one sentence>

<For trivial-fix:>
File: <path>:<line>
Change: <one-line>

<For scoped-feature or epic:>
Lane I — <scope>. Files: <2-4 paths>. Smoke: <1-line>.
Lane II — ...
Lane III — ...

<For research:>
Read these files first: <list>
Then: <plan of investigation>
Output: a markdown doc at /tmp/research-#<issue>.md
```

## Honesty rules

- If the issue is poorly-specified (no acceptance criteria, no example, no "this happens / I expect that"), **classify as `research`** rather than guessing.
- If the issue is a duplicate, classify as `skip` and mention the canonical issue number.
- If you can't tell the difference between scoped and epic, lean **scoped**. We can split into more lanes mid-dispatch if needed.
