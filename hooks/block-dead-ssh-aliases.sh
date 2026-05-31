#!/usr/bin/env bash
# block-dead-ssh-aliases.sh — PreToolUse(Bash)
#
# Blocks ssh / scp / rsync / sftp invocations targeting hosts that look like
# the dead multi-tenant SaaS-era aliases (david, rapali, raj313, miguel-old).
# Sir's memory `dead-saas-ssh-aliases`: those entries still resolve in
# `~/.ssh/config` to defunct boxes; deploying to them is dangerous because
# they look like the live fleet (home, rj, joe, zsolt, miguel, rami).
#
# Live fleet hostnames are public *.alfred.black; dead aliases are bare names.
#
# Configure the dead-alias list by setting ALFRED_CODE_DEAD_ALIASES
# (space-separated). Defaults to Sir's known dead set.
#
# Exit 2 = hard block. Exit 0 = allow.
set -euo pipefail

DEAD="${ALFRED_CODE_DEAD_ALIASES:-david rapali raj313 miguel-old test rapali2}"

input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // ""' 2>/dev/null || true)
[[ -z "$cmd" ]] && exit 0

# Look for ssh/scp/rsync/sftp tokens followed by a dead-alias hostname.
# Match patterns: `ssh david`, `ssh root@david`, `scp file david:/path`,
# `rsync -avz src david:/dst`.
for alias in $DEAD; do
  if echo "$cmd" | grep -qE "(ssh|scp|sftp|rsync)[^|;&]*[[:space:]@]${alias}([[:space:]:]|$)"; then
    {
      echo "BLOCKED: ssh/scp/rsync to dead alias \`${alias}\` (memory: dead-saas-ssh-aliases)."
      echo "That alias points at a defunct SaaS-era box. The live fleet hosts are:"
      echo "  home.alfred.black / rj.alfred.black / joe.alfred.black"
      echo "  zsolt.alfred.black / miguel.alfred.black / rami.alfred.black"
      echo "Use ~/.ssh/alfred-black-verify with -o IdentityAgent=none."
    } >&2
    exit 2
  fi
done

exit 0
