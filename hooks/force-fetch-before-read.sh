#!/usr/bin/env bash
# force-fetch-before-read.sh — UserPromptSubmit
#
# On every new prompt, run `git fetch origin` against the cwd if it's a git
# repo. Caches per-hour so we don't spam the remote.
#
# Closes the stale-local-tree class of bug from 2026-05-30 23:00 UTC:
# I read packages/ctrl/src/api/routes/telegram.ts from a local working tree
# that was 4 hours behind origin/main. Misdiagnosed the per-profile state.
# A `git fetch origin` would have surfaced the drift via `git status -b`.
#
# This hook never blocks. It just side-effects + logs to stderr (visible to
# the next tool call but not surfaced as a refusal).
set -euo pipefail

CACHE_DIR="${HOME}/.cache/alfred-code"
mkdir -p "$CACHE_DIR"

# Only act inside a git working tree.
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi

repo_root=$(git rev-parse --show-toplevel)
repo_id=$(echo "$repo_root" | shasum -a 256 | cut -c1-16)
last_fetch_file="$CACHE_DIR/last-fetch-$repo_id"

now=$(date +%s)
if [[ -f "$last_fetch_file" ]]; then
  last=$(cat "$last_fetch_file" 2>/dev/null || echo 0)
  age=$((now - last))
  if [[ "$age" -lt 3600 ]]; then
    exit 0  # fresh enough
  fi
fi

# Background fetch — if it fails (offline, auth) we ignore.
# 3-second timeout so a slow network never wedges the prompt.
if timeout 3 git fetch origin --quiet 2>/dev/null; then
  echo "$now" > "$last_fetch_file"
  # Surface drift if main is behind.
  if git rev-parse --abbrev-ref --symbolic-full-name @{u} >/dev/null 2>&1; then
    behind=$(git rev-list --count HEAD..@{u} 2>/dev/null || echo 0)
    if [[ "$behind" -gt 0 ]]; then
      echo "alfred-code: $repo_root is $behind commit(s) behind upstream. Pull before reading load-bearing files." >&2
    fi
  fi
fi

exit 0
