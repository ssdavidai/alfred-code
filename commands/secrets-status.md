---
description: Show which canonical secrets are set, which are missing, and where each lives (Keychain vs 1Password). No values, ever.
allowed-tools: Bash
argument-hint: (no args)
---

# /secrets-status

Audit-friendly view of the laptop secrets broker.

## What this prints

```
$ ~/.claude/alfred-code/bin/secret names | while read n; do
    where=$(~/.claude/alfred-code/bin/secret where "$n")
    printf "  %-26s  %s\n" "$n" "$where"
  done
```

Example output:

```
  cloudflare-api-token        keychain
  hetzner-api-token           keychain
  tailscale-api-key           keychain
  openrouter-api-key          1password
  openai-api-key              keychain
  anthropic-api-key           missing
  groq-api-key                missing
  paperclip-api-key           keychain
  pypi-token                  1password
  acme-email                  keychain
  vaultwarden-master          1password
  github-pat                  missing
```

## Optional fingerprint check

If the user wants to verify that the right token is stored (without
revealing it), use the first-6-chars rule from the security memory:

```
~/.claude/alfred-code/bin/secret get cloudflare-api-token | head -c 6
```

This is OK to display. The whole value, never.
