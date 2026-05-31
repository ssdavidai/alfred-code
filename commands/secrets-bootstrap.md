---
description: First-time setup — prompts you for each missing canonical secret and stores it in macOS Keychain. Idempotent; safe to re-run.
allowed-tools: Bash, Read, AskUserQuestion
argument-hint: (no args — interactive)
---

# /secrets-bootstrap

Bring the laptop's secrets broker up to date. The broker is what makes
`secret get <name>` work inline so you never have to paste tokens into
chat again.

## What this does

1. Calls `~/.claude/alfred-code/bin/secret names` to read the canonical
   list of secret names.
2. For each name, checks `secret has <name>` to see if it's already in
   Keychain.
3. For each MISSING name, asks the user (via AskUserQuestion if there
   are 1–4 missing, or one-by-one in the terminal if more) what to do:
   - **Paste value** — runs `secret-set <name>` with the user piping in
     via `--from-stdin`; the value never lands in conversation
   - **Use 1Password ref** — runs `secret-set <name> --1password "<op://…>"`
   - **Skip** — leave it missing; Claude will know to ask later if needed
4. After every successful write, runs `secret list` again to confirm.
5. Prints a one-line summary: N stored, M missing, X via 1Password.

## The protocol

**Never** display secret values back to the user in conversation. The
shell pipe (`echo "$VALUE" | secret-set name --from-stdin`) lets the
user type/paste the value once; everything downstream is opaque.

**Always** use canonical names. If the user wants to store something
not in the canonical list, push back: they should add it to
`bin/secret`'s `canonical_names()` + `docs/secrets-registry.md` first,
THEN run `/secrets-bootstrap` again.

## Run

```bash
~/.claude/alfred-code/bin/secret names \
  | while IFS= read -r name; do
      if ~/.claude/alfred-code/bin/secret has "$name"; then
        printf "  ✓ %s\n" "$name"
      else
        printf "  ✗ %s (missing)\n" "$name"
      fi
    done
```

Then for each missing name, prompt the user with a single AskUserQuestion
batch (group by 4) asking which fill-in method they want, then walk them
through it.

If a user pastes into chat by accident: STOP, tell them to rotate that
token (it's now in the LLM's context window), then continue.
