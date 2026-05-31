#!/usr/bin/env bash
# propose-memory-candidates.sh — Stop
#
# On session end, scan the assistant's transcript for "lesson learned"
# patterns and queue them as memory candidates. User reviews + commits with
# /cleanup-memory.
#
# Heuristic patterns the scan looks for (in assistant turns):
#   - "bit me hard"
#   - "turned out to be"
#   - "the actual bug was"
#   - "honest finding:"
#   - "lesson:"
#   - "surprise:"
#   - "stale [...] caused"
#   - "the trap is"
#   - "memory: <new-name>"
#
# Each match line goes into a candidate file under the project's memory dir.
# The user-facing /cleanup-memory command reads this file and asks Y/N per
# candidate.
#
# Never blocks. Exit 0 always (Stop hook errors are dropped).
set -euo pipefail

# Project's memory dir convention:
#   ~/.claude/projects/<project-slug>/memory/
# Where project-slug is derived from $PWD's hash. We only write if that dir
# already exists (i.e., the user is already using this convention).
project_id=$(echo "$PWD" | shasum -a 256 | cut -c1-16)
mem_dir="${HOME}/.claude/projects/${project_id}/memory"
[[ -d "$mem_dir" ]] || { exit 0; }  # not opted in

candidates_file="$mem_dir/_candidates.md"
input=$(cat)

# Extract assistant turn texts from the JSONL transcript.
# We accept either a flat array of messages or a stream of {role, content[].text}.
assistant_text=$(echo "$input" | jq -r '
  if type == "array" then
    .[] | select(.role == "assistant") | (.content // []) |
      if type == "array" then .[]?.text // empty else . end
  else
    select(.role == "assistant") | (.content // []) |
      if type == "array" then .[]?.text // empty else . end
  end
' 2>/dev/null || echo "")

[[ -z "$assistant_text" ]] && exit 0

# Find lines matching the heuristic patterns.
matches=$(echo "$assistant_text" | grep -iE '\b(bit me hard|turned out to be|actual bug was|honest finding|lesson:|surprise:|the trap is|stale .* caused|memory:[[:space:]]*[a-z])' 2>/dev/null | head -10 || true)

if [[ -n "$matches" ]]; then
  {
    echo
    echo "### Candidates from $(date -u +%FT%TZ)"
    echo "$matches" | sed 's/^/- /'
  } >> "$candidates_file"
  count=$(echo "$matches" | wc -l | tr -d ' ')
  echo "alfred-code: queued $count memory candidate(s) in $candidates_file — run /cleanup-memory to review." >&2
fi

exit 0
