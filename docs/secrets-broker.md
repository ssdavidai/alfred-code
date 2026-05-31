# The secrets broker

## The problem this solves

Every Claude session, the same dance:

> Sir: "deploy that to home.alfred.black"
> Me: "I need the Cloudflare token for the DNS update — could you paste it?"
> Sir: "I already gave it to you yesterday"
> Me: "Right, but I have no memory across sessions"
> Sir: "..."

Even with persistent memory + skills + a project CLAUDE.md, **secret
values can't safely live in any of those** — they end up in
`/Users/ssd/.claude/.../memory/` or a vault entry or worse, in a chat
transcript. Every shop arrives at the same conclusion: put values in a
real key-store, give Claude a CLI to fetch them, never let values land
in plain text.

The broker is two things:

1. **A read CLI** (`bin/secret`) — fetches a value from the canonical
   store and prints it to stdout once. Designed to be used inline:
   `curl -H "Authorization: Bearer $(secret get cloudflare-api-token)"`
2. **A SessionStart hook** (`hooks/inject-secrets-registry.sh`) — every
   fresh Claude session gets a system-reminder listing the canonical
   NAMES of secrets that are currently in the store (never values),
   plus a usage hint.

## Architecture

```
                ┌─────────────────────────────────────────┐
                │  macOS Keychain                          │
                │  (encrypted at rest by FileVault;        │
                │   Touch ID per entry on first use)       │
                └──────┬──────────────────────────────────┘
                       │
                       │ security find-generic-password -s ALFRED_SECRET_<name> -w
                       ↓
                ┌─────────────────────────────────────────┐
                │  bin/secret                              │
                │                                          │
                │  secret get <name>   → stdout            │
                │  secret has <name>   → exit 0/1          │
                │  secret list         → set names only    │
                │  secret names        → all canonical     │
                │  secret where <name> → keychain|1pw|miss │
                └──────┬──────────────────────────────────┘
                       │
                       │ used inline
                       ↓
                ┌─────────────────────────────────────────┐
                │  curl/hcloud/tailscale/wrangler/etc      │
                │                                          │
                │  curl -H "Authorization: Bearer          │
                │    $(secret get cloudflare-api-token)"   │
                │    https://api.cloudflare.com/…          │
                └─────────────────────────────────────────┘
```

The optional 1Password layer (`bin/secret` resolution order 2) lets you
keep some secrets in 1Password instead — useful for things you want
rotated outside the Mac (anything shared across devices). The 1Password
refs live in `~/.alfred-code-state/onepassword-refs.txt` (just
`name=op://...` lines, no values).

## Why Keychain (and not `.env`, not `gopass`, not `bw`)

- `.env` → values land on disk as plain text. FileVault protects
  the laptop's rest state but any script with read access reveals
  the value. No native rotation. No audit trail.
- `gopass` → adds gpg setup overhead. Excellent for teams; overkill
  for one-laptop coordination.
- `bw` (Vaultwarden CLI) → relies on Sir's home.alfred.black tenant
  being up. The whole point of the broker is to remove fragility —
  the fleet shouldn't be a dependency for laptop operations.
- **Keychain** → already exists. Already encrypted. Already has
  Touch ID. Already FileVault-encrypted. `security` ships with every
  macOS install. No daemon to keep alive.

## The invariants

**For the user:**

- Bootstrap once with `/secrets-bootstrap`. After that, Claude knows.
- No env vars in shell profile. No `.envrc`. The broker is the surface.
- Rotation: re-run `secret-set <name>` whenever you rotate; values
  overwrite cleanly.

**For Claude:**

- `secret get <name>` is the only read path. Use inline; never
  intermediate into a shell variable that persists beyond one command.
- `secret list` and `secret names` reveal NAMES; safe to surface.
- `secret get <name> | head -c 6` reveals a 6-char prefix for fingerprint
  diagnosis; safe.
- The full value to stdout/conversation/log: **never.**
- If a name appears in the SessionStart reminder, treat it as "available
  — use it" without asking the user. If a name is missing, that's the
  only time to ask, and the right ask is "could you run
  `/secrets-bootstrap` and add the X token?", not "please paste it here".

## What happens when something rotates

| Secret | Trigger | Recovery |
|---|---|---|
| Cloudflare token | Sir rotates in CF dashboard | `secret-set cloudflare-api-token` (paste new) |
| Hetzner token | Sir rotates in HC dashboard | `secret-set hetzner-api-token` |
| Tailscale auth key | 90d auto-expiry | `secret-set tailscale-api-key` |
| Paperclip board key | 30d auto-expiry | Re-run paperclip onboarding cron + `secret-set paperclip-api-key` |
| GitHub PAT | 90d auto-expiry | Re-run `gh auth login` (preferred); fallback `secret-set github-pat` |

A future `commands/secrets-rotate.md` could drive the rotation flow per
secret. For now, manual is fine — rotation is rare enough.

## Multi-machine

If you start using a second Mac, the Keychain doesn't auto-sync (iCloud
Keychain syncs Safari passwords, not generic-password entries by
default). Options:

1. Run `/secrets-bootstrap` on the new Mac. Slow first-time only.
2. Switch to the 1Password backend for the secrets you want shared.
   `secret-set <name> --1password "op://Personal/<item>/credential"`
   stores a reference, not a value — the value follows wherever your
   1Password is.

## The block-env-dump.sh trip-wire

The Tier 1 hook `block-env-dump.sh` blocks bare `env`, `printenv`,
and pattern-matching variants in `Bash` tool calls. This is the
backstop in case the assistant tries to surface secrets the slow way
(by dumping the whole environment). The broker is the right way; the
hook prevents the wrong way.

If you ever see "the hook blocked your call" in chat, that's working
as designed. Use `secret get <name>` instead.
