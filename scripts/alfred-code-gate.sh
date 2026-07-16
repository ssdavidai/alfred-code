#!/usr/bin/env bash
# alfred-code foreman GATE v2 — emits the SPECIFIC actionable items the foreman
# must act on this tick; SILENT when everything is settled/waiting (→ LLM turn
# skipped). FAIL-OPEN: any error emits work so we never false-skip. GitHub polled
# UNAUTHENTICATED (public repo). The foreman acts ONLY on what this prints.
set -uo pipefail
REPO="ssdavidai/alfred"
PCURL="${PAPERCLIP_API_URL:-http://paperclip:3100/api}"; PCKEY="${PAPERCLIP_API_KEY:-}"
PROJ="2fd3f7f6-2253-41c0-9aaf-2e0d5dba79b9"
work=""
gh(){ curl -s -m 12 -H "Accept: application/vnd.github+json" "https://api.github.com/$1"; }

# ---- issues: flag only those WITHOUT a spec marker ----
iss=$(gh "repos/$REPO/issues?state=open&per_page=50") || { echo "gate: github error — fire"; exit 0; }
nums=$(printf '%s' "$iss" | python3 -c 'import sys,json
try: print(" ".join(str(i["number"]) for i in json.load(sys.stdin) if "pull_request" not in i))
except: print("ERR")' 2>/dev/null)
[ "$nums" = "ERR" ] && { echo "gate: issue-parse error — fire"; exit 0; }
for n in $nums; do
  c=$(gh "repos/$REPO/issues/$n/comments?per_page=100" | grep -c 'alfred-code:spec' 2>/dev/null || true)
  [ "${c:-0}" -eq 0 ] && work="${work}
- issue #${n}: NEEDS SPEC (research + spec + Paperclip issue + build-approval)"
done

# ---- PRs: flag needs-review (no current-SHA marker) or actionable-CI (reviewed, not merge-pending, CI conclusive) ----
prs=$(gh "repos/$REPO/pulls?state=open&per_page=50")
pairs=$(printf '%s' "$prs" | python3 -c 'import sys,json
try: print(" ".join(f"{p[\"number\"]}:{p[\"head\"][\"sha\"]}" for p in json.load(sys.stdin)))
except: print("ERR")' 2>/dev/null)
[ "$pairs" = "ERR" ] && { echo "gate: pr-parse error — fire"; exit 0; }
for ps in $pairs; do
  n="${ps%%:*}"; sha="${ps##*:}"; short="${sha:0:7}"
  comments=$(gh "repos/$REPO/issues/$n/comments?per_page=100")
  rev=$(printf '%s' "$comments" | grep -c "alfred-code:review sha=$sha" 2>/dev/null || true)
  if [ "${rev:-0}" -eq 0 ]; then
    work="${work}
- PR #${n}: NEEDS REVIEW (head ${short} has no review marker)"
  else
    mp=$(printf '%s' "$comments" | grep -c 'alfred-code:merge-pending' 2>/dev/null || true)
    if [ "${mp:-0}" -eq 0 ]; then
      st=$(gh "repos/$REPO/commits/$sha/status" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("state",""))
except: print("")' 2>/dev/null)
      case "$st" in
        success) work="${work}
- PR #${n}: CI GREEN, reviewed, no merge-gate yet (raise merge-approval or dispatch fix)";;
        failure) work="${work}
- PR #${n}: CI RED, reviewed (dispatch codex-builder fix if attempts<3, else HOLD)";;
      esac
    fi
  fi
done

# ---- Paperclip: decided approvals not yet acted on (fail-open if unreadable) ----
if [ -n "$PCKEY" ]; then
  pc=$(curl -s -m 12 -H "Authorization: Bearer $PCKEY" "$PCURL/issues?projectId=$PROJ" 2>/dev/null | python3 -c 'import sys,json
try:
 d=json.load(sys.stdin); items=d if isinstance(d,list) else (d.get("issues") or d.get("data") or [])
 # issues approved-to-build but still unassigned, or merge-approved
 print(sum(1 for i in items if str(i.get("status","")).lower() in ("approved","ready","to_do","todo") and not i.get("assigneeId")))
except: print("ERR")' 2>/dev/null)
  [ "$pc" = "ERR" ] && { echo "gate: paperclip error — fire"; exit 0; }
  [ "${pc:-0}" -gt 0 ] && work="${work}
- Paperclip: ${pc} approved issue(s) awaiting dispatch — assign to codex-feature-builder"
fi

[ -n "$work" ] && printf 'alfred-code: ACTIONABLE ITEMS THIS TICK (act ONLY on these):%s\n' "$work"
# empty stdout = nothing actionable = LLM turn skipped
exit 0
