# Smoke template — MCP server tool

For PRs that add a tool to an MCP server in `packages/mcp-server/src/tools/`. Verifies the tool registers in the catalogue, executes against a real input, and produces audit-able output.

```bash
ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=no root@home.alfred.black 'bash -s' <<'SMOKE'
set -e

# §1 Baseline: tool count before new image
BEFORE=$(docker exec alfred-black-mcp-server-1 sh -c "
  curl -sS http://127.0.0.1:18790/v1/tools 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len([t for t in d.get(\"tools\",[]) if t[\"server\"]==\"<SERVER>\"]))'
" 2>/dev/null || echo "?")
echo "  <SERVER> tools before: $BEFORE"

# §2 Setup: pull + restart mcp-server
docker compose -p alfred-black pull mcp-server 2>&1 | grep -E "Pulled|up to date" | head
docker compose -p alfred-black up -d mcp-server 2>&1 | tail -3
sleep 5

# §3 Mutation: tool is now in the catalogue
docker exec alfred-black-mcp-server-1 sh -c "
  curl -sS http://127.0.0.1:18790/v1/tools | python3 -c \"
import json,sys
d = json.load(sys.stdin)
names = [t['name'] for t in d.get('tools',[]) if t['server']=='<SERVER>']
print('  <SERVER> tools after:', len(names))
print('  includes <NEW_TOOL>:', '<NEW_TOOL>' in names)
print('  catalogue:', sorted(names))
\""

# §4 Isolation assertion: other MCP servers unaffected
docker exec alfred-black-mcp-server-1 sh -c "
  curl -sS http://127.0.0.1:18790/v1/tools | python3 -c \"
import json,sys
from collections import Counter
d = json.load(sys.stdin)
c = Counter(t['server'] for t in d.get('tools',[]))
for s,n in sorted(c.items()): print(f'  {s}: {n}')
\""

# §5 Execute the new tool against a real input
docker exec alfred-black-mcp-server-1 sh -c "
  curl -sS -X POST http://127.0.0.1:18790/v1/tools/call \
    -H 'Content-Type: application/json' \
    -d '{\"name\":\"<NEW_TOOL>\",\"arguments\":{<TEST_ARGS>}}' | python3 -m json.tool" | head -30

# §6 Audit-row presence (if applicable to this tool — some are read-only)
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -H 'Authorization: Bearer \$AAS_API_KEY' \
    'http://127.0.0.1:3100/api/v1/state/audit?target_like=<SERVER>/' | head -c 500" | python3 -m json.tool 2>&1 | head -10
SMOKE
```

**Expected**:
- §1: baseline tool count
- §2: mcp-server pulled + restarted
- §3: tool count incremented + new tool name appears
- §4: other servers' tool counts unchanged
- §5: tool returns sensible output on real args
- §6: audit row present (mutations) or empty (read-only is fine)
