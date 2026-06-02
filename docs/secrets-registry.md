# Secrets registry (canonical names)

The full list of secrets the broker knows about. Every name here is
addressable via `secret get <name>`. Add a new name → also add it to
`bin/secret`'s `canonical_names()` function (single source of truth
otherwise drifts).

## Format

```
<name> | <what it's for> | <where you got it> | <rotation>
```

## The list

| Name | What it's for | Where you got it | Rotate |
|---|---|---|---|
| `cloudflare-api-token` | DNS edit + zone list for `alfred.black` | CF dashboard → My Profile → API Tokens | when leaked |
| `hetzner-api-token` | VM provisioning + Hetzner Cloud project ops | Hetzner Cloud → Project → Security → API Tokens | when leaked |
| `tailscale-api-key` | Tailnet device + ACL management | Tailscale admin → Settings → Keys | 90d |
| `openrouter-api-key` | Model gateway fallback for Hermes | OpenRouter → Keys | when leaked |
| `openai-api-key` | Codex CLI, Whisper, gpt-realtime | OpenAI platform → API keys | when leaked |
| `anthropic-api-key` | Cloud Routines (Tier 4 only — laptop runs use subscription) | Anthropic console → Settings → API Keys | when leaked |
| `groq-api-key` | OMI transcript pipeline | Groq Cloud → Keys | when leaked |
| `composio-api-key` | Composio integrations (Gmail/Calendar OAuth in onboarding) | Composio dashboard → API Keys | when leaked |
| `composio-webhook-secret` | Composio webhook HMAC verification | Composio dashboard | when leaked |
| `agentmail-master-api-key` | AgentMail inbox provisioning + per-profile inbound/outbound email (fleet master key) | AgentMail dashboard → API Keys | when leaked |
| `paperclip-api-key` | Paperclip board key (used by Hermes paperclip adapter) | Paperclip dashboard → MCP / Bootstrap | **30 days** |
| `pypi-token` | `alfred-vault` PyPI publishes (trusted-publishing target) | PyPI → Account settings → Tokens | when used |
| `acme-email` | Operator email used in `init-signup` + LE cert reg on every tenant | the operator-email convention (`david@sabo.tech`) | n/a |
| `vaultwarden-master` | `home.alfred.black` Vaultwarden master password | Set during onboarding | when leaked |
| `github-pat` | Fallback only — `gh auth login` is the primary path | GitHub → Settings → Developer settings → PATs | 90d |
| `alfred-code-github-pat` | alfred-code-on-Paperclip foreman — GitHub API (issues/PRs/merge) on home, stored in home Vaultwarden | GitHub → fine-grained PAT, repo=ssdavidai/alfred, Contents+PRs+Issues RW, Checks+Metadata RO | 90d |
| `telegram-bot-token` | BotFather token for the Sir-facing alert bot (Tier 2 autonomous loop) | @BotFather → `/newbot` → reply contains the token | when leaked |
| `telegram-chat-id` | Sir's chat_id with the bot (captured automatically from `getUpdates`) | First message Sir sends to the bot | n/a (immutable) |

## File paths (not values — just where the file lives)

These aren't in the broker (they're filesystem references), but Claude
should know them and verify they exist:

| Path | Purpose |
|---|---|
| `~/.ssh/alfred-black-verify` | The fleet SSH key. Use with `ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=no root@<host>.alfred.black`. |

The `IdentityAgent=none` flag is mandatory — Sir's 1Password SSH agent
hijacks the connection otherwise (see memory `dead-saas-ssh-aliases`).

## Live fleet hostnames (for SSH allowlist sanity)

```
home.alfred.black
rj.alfred.black
joe.alfred.black
zsolt.alfred.black
miguel.alfred.black
rami.alfred.black
```

The `block-dead-ssh-aliases.sh` hook blocks SSH to any host not in this
list (configurable via `ALFRED_CODE_DEAD_ALIASES` for additions, but the
defaults already cover the live fleet).

## Adding a new secret

1. Add the name to `bin/secret`'s `canonical_names()` heredoc
2. Add a row to this file
3. Run `~/.claude/alfred-code/bin/secret-set <new-name>` to populate it
4. Open a new Claude session — the SessionStart hook will list it

## Removing a secret

1. `security delete-generic-password -s "ALFRED_SECRET_<name>"` to wipe
2. Remove from `bin/secret`'s `canonical_names()` heredoc
3. Remove the row from this file

## Hard rules (for Claude)

- **Never** `env`, `printenv`, `set | grep` to surface secret names.
  The `block-env-dump.sh` hook blocks these but a deliberate workaround
  is a security violation, not a clever solution.
- **Never** echo `$(secret get <name>)` to stdout. Always pipe it
  directly into the curl/cli that needs it.
- **Never** write secret values into commit messages, PR bodies,
  conversation output, log files, or any artefact that lands on disk
  outside Keychain.
- **Echoing prefixes for diagnosis is OK** — `echo "token starts with $(secret get foo | head -c 6)"` is fine for "is it the right token?" debugging. Anything past 6 chars is forbidden.
- **For path-only entries** (like `fleet-ssh-key-path`), Claude can
  state the path in conversation freely — it's not a secret, the key
  *file* is.
