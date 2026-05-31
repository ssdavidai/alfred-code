---
description: "Emit a markdown PM dashboard: open issues, in-flight PRs, fleet drift, pending gates."
allowed-tools: Bash(gh:*, ssh:*, docker:*, jq:*, cat:*, date:*), Read
---

# Open issues

!`gh issue list --state open --limit 30 --json number,title,labels,createdAt --jq '.[] | "  #\(.number)  [\((.labels // []) | map(.name) | join(\",\"))]  \(.title[:80])  (\(.createdAt[:10]))"' | head -20`

# Open PRs

!`gh pr list --state open --limit 20 --json number,title,headRefName,mergeable,createdAt --jq '.[] | "  #\(.number)  [\(.mergeable)]  \(.headRefName)  \(.title[:70])  (\(.createdAt[:10]))"'`

# Recent merges (last 24h)

!`gh pr list --state merged --limit 30 --search "merged:>$(date -u -v-1d +%FT%TZ)" --json number,title,mergedAt --jq '.[] | "  #\(.number)  \(.title[:80])  merged \(.mergedAt[11:19])"' | head -15`

# CI health (last 24h)

!`gh run list --limit 30 --json conclusion,name,createdAt --jq '[.[] | select(.createdAt > (now - 86400 | strftime("%Y-%m-%dT%H:%M:%SZ")))] | group_by(.name) | map({name: .[0].name, total: length, fail: map(select(.conclusion == "failure")) | length}) | .[] | "  \(.name): \(.total) runs, \(.fail) failures"'`

# Pending gates (autonomous loop)

!`cat ~/.alfred-code-state/pending-gates.json 2>/dev/null | jq -r 'to_entries | map(select(.value.status == "awaiting")) | .[] | "  gate \(.key) → issue #\(.value.issue) (\(.value.triage))"' | head -10`

# Currently dispatched issues

!`cat ~/.alfred-code-state/dispatched.json 2>/dev/null | jq -r 'to_entries | map(select(.value.status == "dispatched")) | .[] | "  issue #\(.value.issue) → \(.value.dispatched_at)"' | head -10`

---

You are the **PM dashboard renderer**. Take the inputs above and emit a clean, Sir-readable markdown summary. Group by:

```markdown
# PM dashboard — $(date -u +%FT%TZ)

## What's on Sir's plate
- N issues awaiting Sir's tap to dispatch (list with one-line decomp)
- N PRs awaiting Sir's review (list with smoke status if known)
- N PRs blocking on smoke evidence (list with what's missing)

## What's running
- N lanes mid-flight (issue → lane → file count → last activity)

## What shipped today
- N PRs merged (group by issue if multiple lanes merged on the same issue)
- N issues closed

## CI health
- Build pipeline status, flake observation

## Fleet
- (only fetch if Sir is asking for fleet check — it requires SSH and isn't free)

## Notes
- Anything anomalous worth surfacing
```

Honest reporting:
- If nothing happened today, say "Nothing landed today; loop ran N times, all quiet." — don't manufacture activity.
- If a PR has been open >24h without progress, flag it (often means it's stuck).
- If a gate has been awaiting >2h, flag it (Sir may have missed the Telegram ping).
