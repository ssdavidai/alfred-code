---
description: "Pull latest images on every tenant in the fleet"
allowed-tools: Bash(ssh:*, docker:*, gh:*)
argument-hint: "[service-name…  default: ctrl-api alfred-learn web web-client mcp-server hermes]"
---

# Sanity: verify SSH key exists

!`ls -la ~/.ssh/alfred-black-verify 2>/dev/null || echo "MISSING — fleet pull will fail"`

# Latest build runs on main (sanity for what we're about to roll)

!`gh run list --workflow=build-ctrl-api.yml --branch=main --limit 1 --json conclusion,createdAt --jq '.[] | "ctrl-api build: \(.conclusion) at \(.createdAt)"'`
!`gh run list --workflow=build-web.yml --branch=main --limit 1 --json conclusion,createdAt --jq '.[] | "web build:      \(.conclusion) at \(.createdAt)"'`
!`gh run list --workflow=build-learn.yml --branch=main --limit 1 --json conclusion,createdAt --jq '.[] | "learn build:    \(.conclusion) at \(.createdAt)"'`

---

# Plan

You are about to pull-and-restart Docker images on the live tenant fleet.

## Fleet hosts (live, *.alfred.black)

- `home.alfred.black` — Sir's daily driver
- `rj.alfred.black`
- `joe.alfred.black` — has Cratchit sibling; respect [[durable-tenant-customization-pattern]] memory before changing compose
- `zsolt.alfred.black`
- `miguel.alfred.black`
- `rami.alfred.black` — newest; provisioned 2026-05-30

## Default service set

If no services were passed via `$ARGUMENTS`, pull the standard set:
- `ctrl-api`
- `alfred-learn`
- `web`
- `web-client`
- `mcp-server`
- `hermes`

## SSH form (mandatory shape)

```
ssh -i ~/.ssh/alfred-black-verify \
    -o IdentityAgent=none \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    root@<host>.alfred.black
```

## Execution

For each tenant, in serial (not parallel — easier to debug if one fails):

```
ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=no root@<host>.alfred.black \
  "cd /opt/alfred && docker compose -p alfred-black pull <services> 2>&1 | grep -E 'Pulled|up to date' && docker compose -p alfred-black up -d <services>"
```

After each tenant: a one-line status (✓ or ✗ with the error).

After all tenants: a coverage matrix showing which services landed where.

## Gotchas

- `joe.alfred.black` has a `cratchit` sibling profile via `docker-compose.override.yaml`. Do NOT pull anything that would break Cratchit (the cdsk MCP server). If you're pulling `hermes` on joe, double-check the override is still applied after `up -d`.
- `dead SSH aliases` (david, rapali, raj313) are blocked by the hook — if you accidentally type one of those, the block-dead-ssh-aliases hook will catch you.
- If `docker compose pull` reports "no such service", the override file may have renamed something. Investigate before retrying.
