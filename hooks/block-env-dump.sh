#!/usr/bin/env bash
# block-env-dump.sh — PreToolUse(Bash)
#
# Blocks any `env` invocation that would dump the full environment to stdout
# where it could leak to the conversation. Sir's memory note `never-env-dump`
# (2026-05-29) documents the rule: a single mis-grepped `env` dumped
# CODEX_BUILDER_DEPLOY_KEY + OPENAI/OPENROUTER/GROQ/PAPERCLIP/VEXA tokens and
# every POSTGRES password in one go.
#
# Detection rule: any command-line token sequence that is bare `env` followed
# by EOL, whitespace, or pipe. We allow `env VAR=value cmd` (the legit
# variable-assignment shape) and `printenv`/`env -i` (no dump).
#
# Exit codes per Claude Code hook protocol:
#   0  → allow
#   2  → hard block (stderr surfaces to the LLM as a refusal reason)
set -euo pipefail

input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // ""' 2>/dev/null || true)
[[ -z "$cmd" ]] && exit 0

# Reject `env`, `env|...`, `env 2>...`, etc. — but ALLOW `env VAR=...`.
# The negative lookbehind we want is "env not followed by [SPACE]<UPPER>=value"
# so we split on shell separators and check each segment's first token.
echo "$cmd" | tr ';&|' '\n' | while IFS= read -r segment; do
  first=$(echo "$segment" | awk '{print $1}')
  rest=$(echo "$segment" | awk '{$1=""; print $0}' | sed 's/^ *//')
  if [[ "$first" == "env" ]]; then
    # Allow: `env -i`, `env --unset`, `env VAR=value cmd`
    if echo "$rest" | grep -qE '^(-i\b|-u\b|--unset|--ignore-environment|--null|[A-Za-z_][A-Za-z0-9_]*=)' ; then
      continue
    fi
    # Bare `env` or `env | grep ...` — block.
    {
      echo "BLOCKED: bare \`env\` dump detected (memory: never-env-dump)."
      echo "Even with a grep filter the dump can land in the conversation if"
      echo "the filter fails. Use one of these safe forms instead:"
      echo "  printenv VARNAME            # one var by name"
      echo "  env | awk -F= '{print \$1}' # names only, no values"
    } >&2
    exit 2
  fi
done

exit 0
