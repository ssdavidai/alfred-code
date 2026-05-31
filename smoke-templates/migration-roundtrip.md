# Smoke template — state.db migration round-trip

For PRs that add a `packages/ctrl/src/db/migrations/NNNN_*.sql`. Verifies `user_version` bumps + the new tables are queryable + the migration is idempotent on re-run.

```bash
ssh -i ~/.ssh/alfred-black-verify -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=no root@home.alfred.black 'bash -s' <<'SMOKE'
set -e

# §1 Baseline: current user_version BEFORE the new image is pulled
BEFORE=$(docker exec alfred-black-ctrl-api-1 node -e "
const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync(process.env.STATE_DB_PATH,{readOnly:true});
console.log(db.prepare('PRAGMA user_version').get().user_version);
" 2>/dev/null)
echo "  user_version before: $BEFORE"

# §2 Setup: pull + restart ctrl-api with the new migration
docker compose -p alfred-black pull ctrl-api 2>&1 | grep -E "Pulled|up to date" | head
docker compose -p alfred-black up -d ctrl-api 2>&1 | tail -3
sleep 8

# §3 Mutation: user_version should have bumped
AFTER=$(docker exec alfred-black-ctrl-api-1 node -e "
const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync(process.env.STATE_DB_PATH,{readOnly:true});
console.log(db.prepare('PRAGMA user_version').get().user_version);
")
echo "  user_version after:  $AFTER"
[[ "$AFTER" -gt "$BEFORE" ]] && echo "  ✓ bumped"

# §4 Isolation assertion: new tables exist + seed rows present (replace <TABLE> with the migration's table)
docker exec alfred-black-ctrl-api-1 node -e "
const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync(process.env.STATE_DB_PATH,{readOnly:true});
const tabs = db.prepare(\"SELECT name FROM sqlite_master WHERE type='table' AND name IN ('<TABLE>')\").all();
console.log('  tables present:', tabs.map(t=>t.name).join(', '));
const rows = db.prepare('SELECT * FROM <TABLE> LIMIT 5').all();
console.log('  seed rows:', rows.length);
for (const r of rows) console.log('    ', JSON.stringify(r));
"

# §5 Audit-row presence: migration application was logged
docker logs alfred-black-ctrl-api-1 --tail 100 2>&1 | grep -iE "migration.*<MIGRATION_NUM>|user_version.*$AFTER" | head -5

# §6 Idempotency: re-running migrations doesn't break anything
docker compose -p alfred-black restart ctrl-api 2>&1 | tail -2
sleep 5
REPEAT=$(docker exec alfred-black-ctrl-api-1 node -e "
const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync(process.env.STATE_DB_PATH,{readOnly:true});
console.log(db.prepare('PRAGMA user_version').get().user_version);
")
[[ "$REPEAT" == "$AFTER" ]] && echo "§6 ✓ user_version stable on restart ($REPEAT)"
SMOKE
```

**Expected**:
- §1: a non-empty integer (current user_version)
- §2: ctrl-api pulled + restarted cleanly
- §3: user_version > before
- §4: new tables present, seed rows visible
- §5: a log line mentions the migration number
- §6: user_version stable across restart (proves idempotency)
