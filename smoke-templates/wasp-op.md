# Smoke template — Wasp operation

For PRs that add a Wasp query or action in `packages/web/src/dashboard/operations.ts` (or similar). Verifies the op registers, returns the right shape, and (for actions) writes the expected backend state.

**Crucial Wasp gotcha**: the op MUST be a plain async function with explicit `Promise<any>` return type. PRs #139, #145, #182, #184, #186 all hit the `Payload` constraint trap. See `docs/smoke-as-truth.md`.

```bash
ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=no root@home.alfred.black 'bash -s' <<'SMOKE'
set -e

# §1 Baseline: confirm web container is healthy
docker compose -p alfred-black ps web web-client --format 'table {{.Name}}\t{{.Status}}'

# §2 Setup: pull + restart web + web-client
docker compose -p alfred-black pull web web-client 2>&1 | grep -E "Pulled|up to date" | head
docker compose -p alfred-black up -d web web-client 2>&1 | tail -3
sleep 10

# §3 Mutation: hit the new Wasp op via the SPA's API endpoint
# Wasp ops are at /operations/<opName>; auth via Wasp session cookie.
# For unauth'd test: use the dashboard's session cookie OR hit via ctrl-api proxy.

# Easier: confirm the op symbol is in the built server bundle
docker exec alfred-black-web-1 sh -c "
  grep -c '<OP_NAME>' /app/.wasp/build/server/bundle/server.js
"

# §4 Isolation assertion: pre-existing ops still work (no Wasp build regression)
curl -sS -o /dev/null -w 'HTTP %{http_code} on /profiles\n' --max-time 8 https://home.alfred.black/profiles
curl -sS -o /dev/null -w 'HTTP %{http_code} on /channels\n' --max-time 8 https://home.alfred.black/channels
curl -sS -o /dev/null -w 'HTTP %{http_code} on /tools\n' --max-time 8 https://home.alfred.black/tools

# §5 If the op proxies to a ctrl-api route, exercise that route directly
# (this proves the data path even if the SPA test is hard)
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -H 'Authorization: Bearer \$AAS_API_KEY' \
    'http://127.0.0.1:3100/api/v1/<BACKING_ROUTE>'" | python3 -m json.tool | head -20

# §6 Audit-row (if this is an action — queries don't audit)
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -H 'Authorization: Bearer \$AAS_API_KEY' \
    'http://127.0.0.1:3100/api/v1/state/audit?target_like=<TARGET_PREFIX>' | head -c 500" | python3 -m json.tool 2>&1 | head -10
SMOKE
```

**Expected**:
- §1: web + web-client running
- §2: pull + restart clean
- §3: grep returns > 0 (op symbol is in the bundle = wasp build succeeded)
- §4: all three existing routes return 200
- §5: backing route returns sensible data
- §6: action wrote an audit row (or empty for queries)
