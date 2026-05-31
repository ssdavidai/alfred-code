# Smoke template — channel-token route

For PRs that add a per-profile credential route on a channel (telegram bot token, slack workspace tokens, etc). Replace `<KIND>` and `<SLUG>` with the actual channel kind + a test profile slug.

```bash
ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=no root@home.alfred.black 'bash -s' <<'SMOKE'
set -e

# §1 Baseline: main's <KIND> token snapshot (so we can prove no clobber)
docker exec alfred-black-hermes-1 sh -c "grep -c '^<KIND>_TOKEN=' /hermes-state/profiles/main/.env || echo 0"

# §2 Setup: create test profile + wait for running
docker exec alfred-black-ctrl-api-1 sh -c '
  curl -sS -X POST -H "Authorization: Bearer $AAS_API_KEY" -H "Content-Type: application/json" \
    -d "{\"slug\":\"smoke-<SLUG>\",\"label\":\"Smoke <SLUG>\",\"model\":\"x-ai/grok-4.3\"}" \
    http://127.0.0.1:3100/api/v1/agent-profiles' > /dev/null
for i in $(seq 1 30); do
  st=$(docker exec alfred-black-ctrl-api-1 sh -c '
    curl -sS -H "Authorization: Bearer $AAS_API_KEY" \
      http://127.0.0.1:3100/api/v1/agent-profiles/smoke-<SLUG>' | python3 -c "import json,sys; print(json.load(sys.stdin)['profile']['status'])" 2>/dev/null || echo unknown)
  [ "$st" = "running" ] && echo "§2 ✓ running" && break
  sleep 2
done

# §3 Mutation: PUT smoke-<SLUG>'s <KIND> token via the new route
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -X PUT -H 'Authorization: Bearer \$AAS_API_KEY' -H 'Content-Type: application/json' \
    -d '{\"token\":\"FAKE-<KIND>-TOKEN-FOR-SMOKE-PLACEHOLDER\"}' \
    'http://127.0.0.1:3100/api/v1/channels/<KIND>/token?profile=smoke-<SLUG>'" | python3 -m json.tool

# §4 Isolation assertion: smoke-<SLUG>'s .env has the new token; main's UNCHANGED
docker exec alfred-black-hermes-1 sh -c "
  echo '  smoke-<SLUG>:' \$(grep -c '^<KIND>_TOKEN=' /hermes-state/profiles/smoke-<SLUG>/.env 2>/dev/null || echo 0)
  echo '  main:       ' \$(grep -c '^<KIND>_TOKEN=' /hermes-state/profiles/main/.env 2>/dev/null || echo 0)
"

# §5 Audit-row presence
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -H 'Authorization: Bearer \$AAS_API_KEY' \
    'http://127.0.0.1:3100/api/v1/state/audit?target_like=channels/<KIND>/' | head -c 800" | python3 -m json.tool 2>&1 | head -10

# §6 Cleanup: DELETE smoke-<SLUG>'s token + archive profile
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -X DELETE -H 'Authorization: Bearer \$AAS_API_KEY' \
    'http://127.0.0.1:3100/api/v1/channels/<KIND>/token?profile=smoke-<SLUG>'" > /dev/null
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -X DELETE -H 'Authorization: Bearer \$AAS_API_KEY' \
    http://127.0.0.1:3100/api/v1/agent-profiles/smoke-<SLUG>" > /dev/null
echo "§6 ✓ cleanup done"
SMOKE
```

**Expected**:
- §1: baseline count
- §2: status=running within 30s
- §3: 200 with the new state
- §4: smoke-<SLUG> count = 1, main count = baseline (no clobber)
- §5: audit row with `payload_json.profile_slug=smoke-<SLUG>`
- §6: silent
