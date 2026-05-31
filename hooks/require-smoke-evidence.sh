#!/usr/bin/env bash
# require-smoke-evidence.sh — PreToolUse(Bash)
#
# Blocks `gh pr merge` invocations against PRs whose body lacks a
# "## Smoke evidence" section. The lesson is encoded in
# `docs/smoke-as-truth.md`: lint passing isn't shipping, smoke is.
#
# Workflow: when an agent prepares to merge a PR, we fetch the PR body via
# `gh pr view <n> --json body` and grep for the required heading. If absent,
# block + tell the agent to either add it or use --admin with a documented
# reason (memory: documented-ci-flakes — compose-lint, test-voice-bridge).
#
# Allow override with `ALFRED_CODE_SKIP_SMOKE_GATE=1` for emergencies.
#
# Exit 2 = block. Exit 0 = allow.
set -euo pipefail

if [[ "${ALFRED_CODE_SKIP_SMOKE_GATE:-0}" == "1" ]]; then
  exit 0
fi

input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // ""' 2>/dev/null || true)
[[ -z "$cmd" ]] && exit 0

# Only act on `gh pr merge ...`.
echo "$cmd" | grep -qE '\bgh[[:space:]]+pr[[:space:]]+merge\b' || exit 0

# Extract the PR number — the first integer after `merge`.
pr_num=$(echo "$cmd" | sed -nE 's/.*\bgh[[:space:]]+pr[[:space:]]+merge[[:space:]]+([0-9]+).*/\1/p')
if [[ -z "$pr_num" ]]; then
  # `gh pr merge` without a number — operating on the current branch's PR.
  # Resolve via gh:
  pr_num=$(gh pr view --json number --jq '.number' 2>/dev/null || echo "")
fi

[[ -z "$pr_num" ]] && exit 0  # can't resolve, don't block

# Fetch PR body.
body=$(gh pr view "$pr_num" --json body --jq '.body' 2>/dev/null || echo "")

if [[ -z "$body" ]]; then
  exit 0  # can't fetch; don't block
fi

if echo "$body" | grep -qE '^##[[:space:]]+Smoke evidence' || \
   echo "$body" | grep -qE '^###[[:space:]]+Smoke evidence' || \
   echo "$body" | grep -qE '^##[[:space:]]+Smoke verification' || \
   echo "$body" | grep -qE '\bSmoke (transcript|evidence|verified):' ; then
  exit 0  # smoke evidence present
fi

# No smoke evidence section. Block.
{
  echo "BLOCKED: PR #$pr_num body is missing a \`## Smoke evidence\` section."
  echo "Tonight's standing rule (docs/smoke-as-truth.md): functional smoke"
  echo "is the merge gate, not lint."
  echo ""
  echo "Add a section to the PR body with the verbatim smoke output. Shape:"
  echo "  ## Smoke evidence"
  echo "  Tested on home.alfred.black at <UTC>."
  echo "  \`\`\`"
  echo "  §1 baseline: <result>"
  echo "  §2 mutation: <result>"
  echo "  §3 isolation check: <result>"
  echo "  ...etc..."
  echo "  \`\`\`"
  echo ""
  echo "If this is a docs-only PR where smoke doesn't apply, mark with"
  echo "  ## Smoke evidence"
  echo "  N/A — docs-only change."
  echo ""
  echo "For documented CI flakes that prevent CI signal, you can still merge"
  echo "with \`gh pr merge --admin\` AFTER the smoke evidence is in the body."
  echo "Emergency override: ALFRED_CODE_SKIP_SMOKE_GATE=1"
} >&2
exit 2
