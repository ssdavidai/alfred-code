# Smoke template — Temporal workflow trigger

For PRs that add a workflow in `packages/learn/src/workflows/`. Replace `<WORKFLOW_NAME>` and `<WORKFLOW_KIND>` with concrete names. The 6 sections all run on `home.alfred.black`.

```bash
ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=no root@home.alfred.black 'bash -s' <<'SMOKE'
set -e

# §1 Baseline: no <WORKFLOW_NAME> currently running
docker exec alfred-black-alfred-learn-1 sh -c '
  curl -sS http://127.0.0.1:8788/health || echo "alfred-learn sidecar not reachable"
'

# §2 Setup: assert the workflow class is registered
docker exec alfred-black-alfred-learn-1 python3 -c "
from src.registry import REGISTERED_WORKFLOWS
print('  <WORKFLOW_NAME> registered:', '<WORKFLOW_NAME>' in [w.__name__ for w in REGISTERED_WORKFLOWS])
"

# §3 Mutation: trigger the workflow via ctrl-api (or direct Temporal client)
WF_ID=$(docker exec alfred-black-ctrl-api-1 sh -c '
  curl -sS -X POST -H "Authorization: Bearer $AAS_API_KEY" -H "Content-Type: application/json" \
    -d "{\"workflow\":\"<WORKFLOW_NAME>\",\"args\":{}}" \
    http://127.0.0.1:3100/api/v1/workflows/start' | python3 -c "import json,sys; print(json.load(sys.stdin).get('workflow_id',''))")
echo "  workflow_id: $WF_ID"

# §4 Isolation assertion: only this workflow ran, no side effects on other workflows
# Watch for ~30s for the workflow to complete
for i in $(seq 1 15); do
  sleep 2
  status=$(docker exec alfred-black-ctrl-api-1 sh -c "
    curl -sS -H 'Authorization: Bearer \$AAS_API_KEY' \
      http://127.0.0.1:3100/api/v1/workflows/$WF_ID/status" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo unknown)
  echo "  iter=$i status=$status"
  [[ "$status" == "COMPLETED" ]] && break
  [[ "$status" == "FAILED" ]] && { echo "WORKFLOW FAILED"; break; }
done

# §5 Audit-row presence: the workflow's outcome was journaled
docker exec alfred-black-ctrl-api-1 node -e "
const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync(process.env.STATE_DB_PATH,{readOnly:true});
const row = db.prepare(\"SELECT id, action, created_at FROM audit_log WHERE target LIKE 'workflow/%' ORDER BY rowid DESC LIMIT 3\").all();
for (const r of row) console.log('  audit:', JSON.stringify(r));
"

# §6 Cleanup: terminate any leftover runs
docker exec alfred-black-ctrl-api-1 sh -c "
  curl -sS -X DELETE -H 'Authorization: Bearer \$AAS_API_KEY' \
    http://127.0.0.1:3100/api/v1/workflows/$WF_ID" > /dev/null 2>&1 || true
echo "§6 ✓ cleanup done"
SMOKE
```

**Expected**:
- §1: alfred-learn sidecar reachable
- §2: workflow registered
- §3: workflow_id returned
- §4: COMPLETED within 30s, no other workflows side-effected
- §5: audit row present
- §6: silent
