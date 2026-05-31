---
description: "Provision a new alfred tenant VM end-to-end: Hetzner cx33 → Cloudflare DNS → Docker → ssdavidai/alfred stack → verify. Operator-local; uses the secret broker."
allowed-tools: Bash(curl:*, ssh:*, dig:*, jq:*, ssh-keygen:*), Read
argument-hint: "<subdomain> [--size cx33] [--location fsn1] [--fidelity smoke|full]"
---

# Provision a new alfred tenant — `<subdomain>.alfred.black`

This is the **operator-local** provisioning path. The CLI provisioner was pruned
from the public `ssdavidai/alfred` repo at the public launch (commit `54d7c80d`);
it survives only in the dead `alfred-platform` monorepo. This skill replaces it —
it does the manual Hetzner-API + `bootstrap.sh` flow that built `staging.alfred.black`.
See the `tenant-provisioning-path` memory for the full background.

**Never put this logic in the public repo.** It's how *you* operate the fleet,
not part of the product.

## Inputs
- `$1` = subdomain (e.g. `staging`, `demo`, `alice`). Tenant served at `https://$1.alfred.black`.
- `--size` (default `cx33` — NOTE: **cx32 does not exist** in Hetzner; cx33 = 4vCPU/8GB/80GB)
- `--location` (default `fsn1`)
- `--fidelity` (default `smoke`): `smoke` stubs integration secrets (Composio/AgentMail/Groq/voice); `full` prompts for real ones.

## Secrets — all via the broker, never echo values
`secret get hetzner-api-token | cloudflare-api-token | acme-email | tailscale-api-key | openrouter-api-key`. Recover any missing one from `~/.claude/projects/-Users-ssd-dev-alfred-platform/*.jsonl` + validate against the live API before storing (see how `staging` recovered openrouter).

## Steps

### 1. Create the VM (Hetzner API)
```bash
HZ=$(secret get hetzner-api-token)
curl -s -X POST -H "Authorization: Bearer $HZ" -H "Content-Type: application/json" \
  https://api.hetzner.cloud/v1/servers -d '{
    "name":"<sub>-alfred-black","server_type":"cx33","image":"ubuntu-24.04",
    "location":"fsn1","ssh_keys":["alfred-black-verify"],
    "labels":{"role":"<smoke|tenant>","fleet":"alfred-black"},"start_after_create":true}'
```
Record the returned `id` (for teardown) + `public_net.ipv4.ip`. The `alfred-black-verify`
key is already registered in Hetzner → SSH with `~/.ssh/alfred-black-verify`.

### 2. DNS (Cloudflare API, zone `f13033654094bba0fdfb4c5605496e47`)
Delete any stale record for `<sub>.alfred.black`, then create **A** `<sub>.alfred.black`
+ **A** `*.<sub>.alfred.black` → the new IP, `proxied:false` (so Caddy gets LE certs and
SSH is reachable). Auth header pair: `X-Auth-Email: $(secret get acme-email)` +
`X-Auth-Key: $(secret get cloudflare-api-token)` (it's a CF Global key).

### 3. OS prep + clone (SSH to the new IP, retry ≤3 min for first boot)
```bash
ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes \
    -o StrictHostKeyChecking=no root@<ip> '
  curl -fsSL https://get.docker.com | sh
  git clone https://github.com/ssdavidai/alfred /opt/alfred && cd /opt/alfred
  cp .env.example .env'
```

### 4. Fill the `USER MUST FILL` block in `/opt/alfred/.env`
Required real: `DOMAIN=<sub>.alfred.black`, `ACME_EMAIL`, `OWNER_NAME`, `OWNER_EMAIL`,
`OPENROUTER_API_KEY` (Hermes routes through it — Anthropic optional/can be blank).
For `--fidelity smoke`: set `COMPOSIO_API_KEY / GROQ_API_KEY / MAILGUN_API_KEY` =
`stub-not-configured`; leave `ANTHROPIC / GOOGLE_CLIENT_* / SENDGRID / AGENTMAIL_*`
blank; `SURE_ENABLED=false`, `TAILSCALE_ENABLED=false`. For `--fidelity full`: prompt
Sir / recover real keys for each.

### 5. Bootstrap + bring up
```bash
ssh ... root@<ip> 'cd /opt/alfred && bash scripts/bootstrap.sh && docker compose up -d'
```
For a smoke tenant, bring up the **core set** first so optional stacks can't block it:
`caddy web web-client web-db ctrl-api init temporal hermes alfred alfred-learn mcp-server vaultwarden`.
Plane/sure/paperclip/voice-bridge are opt-in (voice-bridge crash-loops without
`OPENAI_API_KEY` + `VOICE_BRIDGE_INTERNAL_TOKEN` — leave it down on smoke).

### 6. Verify before declaring done
- `curl -sS -o /dev/null -w '%{http_code}' https://<sub>.alfred.black/` → 200 (Caddy may need ~1 min for LE)
- `https://<sub>.alfred.black/api/v1/health` → 200
- `docker compose ps` core services `Up (healthy)`

### 7. Report
New IP, server id, HTTPS code, services Up vs stubbed-down, the exact stub list,
the teardown command, and the running cost (cx33 ≈ €16/mo).

## Teardown
```bash
curl -s -X DELETE -H "Authorization: Bearer $(secret get hetzner-api-token)" \
  https://api.hetzner.cloud/v1/servers/<id>
# then delete the CF DNS records for <sub>.alfred.black + *.<sub>.alfred.black
```

## Hard rules
- Only ever create/modify the NEW box. NEVER touch home/rj/joe/zsolt/miguel/rami.
- Never echo secret values (6-char prefix max for diagnosis).
- This spends money — confirm with Sir before step 1 unless he already said go.
