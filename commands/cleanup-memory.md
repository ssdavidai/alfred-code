---
description: "Review memory candidates queued by propose-memory-candidates.sh and commit selected ones"
allowed-tools: Read, Write, Edit, AskUserQuestion, Bash(ls:*, cat:*, mv:*, rm:*)
---

# Find the candidates file for this project

!`project_id=$(echo "$PWD" | shasum -a 256 | cut -c1-16); cf="$HOME/.claude/projects/$project_id/memory/_candidates.md"; if [ -f "$cf" ]; then echo "candidates: $cf"; cat "$cf"; else echo "no candidates queued for this project ($cf)"; fi`

# Existing memories for reference

!`project_id=$(echo "$PWD" | shasum -a 256 | cut -c1-16); ls -la "$HOME/.claude/projects/$project_id/memory/" 2>/dev/null | grep -v _candidates | head -30`

---

If the candidates file is empty or absent, say so and stop. There's nothing to clean up.

Otherwise:

## 1. Group the candidates

Read each candidate line. Group near-duplicates that describe the same lesson (e.g. three lines about the `tini -g` SIGUSR1 broadcast → one memory).

## 2. Propose 0-N memory drafts

For each unique candidate, draft a memory file. Shape:

```markdown
---
name: <short-kebab-case-slug>
description: <one-line summary — used to decide relevance during recall>
metadata:
  type: feedback   # or user | project | reference per the project's convention
---

<the fact; for feedback/project, follow with **Why:** and **How to apply:** lines.
Link related memories with [[their-name]].>
```

Check existing memories first — **update an existing one** if the new candidate refines or extends it, rather than creating a duplicate.

## 3. Ask Sir Y/N per candidate

Use `AskUserQuestion` with one question per drafted memory. Shape:

```
Question: "Save this as a memory?"
Header: "<slug>"
Options:
  - Save (Recommended)  → write the file + add the MEMORY.md pointer
  - Edit first          → show the draft, let Sir edit, then save
  - Skip                → drop the candidate
```

## 4. After the answers

For each "Save":
- Write the file to `~/.claude/projects/<project>/memory/<slug>.md`
- Append a one-line pointer to `MEMORY.md` in the format the project already uses:
  ```
  - [<title>](<slug>.md) — <one-line hook>
  ```

For "Edit first":
- Show the draft, let Sir paste a corrected version, then save.

## 5. Empty the candidates file

Once all candidates are processed, remove `_candidates.md` so the next session starts fresh.

## Honesty rule

If a candidate doesn't actually contain a durable lesson — it's just chatter, or it duplicates an existing memory perfectly — **say so and skip it**. The memory store gets noisy if every "huh" becomes a file.
