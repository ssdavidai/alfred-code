# Smoke as truth

Lint passing is not shipping. Smoke is.

This rule exists because **every load-bearing bug tonight was caught by a smoke step, not by tests.**

| Bug | What lint said | What smoke caught |
|---|---|---|
| `tini -g` broadcasts SIGUSR1 to every gateway | ✓ shellcheck clean | Sentinel's token-write bounced main's gateway |
| SQLite `immutable=1` flag silently misses WAL rows | ✓ tests passed (no WAL fixture) | Newly-created profile invisible to the supervisor's render_registry |
| Gateway-token (43ch) ≠ AAS_API_KEY (64ch) on supervisor probe | ✓ all units green | First per-profile status callback 401'd |
| AgentMail inbox-scoped keys can't create new inboxes | ✓ provisioning code reads clean | Live provision returned 401 against the real API |
| `tini` undoes PR #118's `chmod 0o711` after every restart | ✓ chmod ran | uid 1000 EACCES on `.env` after the first restart cycle |

In each case the design intent — *"per-profile X is isolated from Y"* — required exercising the actual runtime against a real-state fixture, not just running tests.

# The 6 mandatory sections

Every functional smoke needs:

1. **Baseline** — assert no-regression for `main` (or whatever the existing surface is)
2. **Setup** — create the test artifact (a profile, a fixture, a webhook payload)
3. **Mutation** — exercise the actual route / workflow / tool
4. **Isolation assertion** — explicitly check that the change DOESN'T leak (other profiles unaffected, other files unchanged, other sessions intact)
5. **Audit-row presence check** — verify the audit ledger has the correct row with the correct payload
6. **Cleanup** — no orphan rows / dirs / files on `home.alfred.black`

The isolation assertion (§4) is the one that catches the design-intent bugs. Skip it and you ship `tini -g`-class regressions.

# What counts as a smoke step

| Counts | Doesn't count |
|---|---|
| Real HTTP call against `home.alfred.black`'s ctrl-api | Mocked HTTP call in a unit test |
| `docker exec` reading a file from the live container | `cat` against a fixture file in the repo |
| A binding row read from `state.db` post-mutation | A `SELECT` against a sqlite fixture |
| The `audit_log` table has a row with `profile_slug=<x>` | "the route writes an audit row" (claim without check) |
| `docker compose ps` reports a service healthy after restart | "the service should restart cleanly" (no probe) |

# What honest partials look like

If §4 (isolation) or §5 (audit) fails, the lane is **partial**, not done.

Honest partial reporting shape:

```
Lane <N>: PARTIAL — §4 isolation assertion failed.
  What worked: §1, §2, §3, §6.
  What didn't: setting sentinel's TELEGRAM_BOT_TOKEN also overwrote main's
    .env line because writeProfileEnvKeys() was called without the resolved
    paths arg.
  Root cause: PR #197 parametrized the writer signature but the PUT route
    handler at line 561 still calls the old shape.
  Recommended fix: re-dispatch a lane to thread `paths` through the route
    handler, with the smoke re-running §4.
```

That's a partial. **Not** "PR #N merged, lane done" with a smoke transcript that only ran §1-§3.

# When smoke is impossible

Some operator steps genuinely can't be automated:
- Sir entering a real Twilio bot token (requires a Twilio account purchase)
- Sir adding `AGENTMAIL_MASTER_API_KEY` to a tenant's `/opt/alfred/.env`
- Sir clicking "Connect" on a Tailscale device-link page

In those cases, the smoke proves the **routing decision** end-to-end (mock the webhook payload, assert it routes to the right profile + gateway + key). The real-call test is then surfaced as an **operator step** in the PR body and CHANGELOG.

That's still better than no smoke — the routing decision is the load-bearing part.

# The hook that enforces this

`hooks/require-smoke-evidence.sh` blocks `gh pr merge <n>` if the PR body lacks `## Smoke evidence`. The block message tells the agent the exact section shape it needs. Emergency override is `ALFRED_CODE_SKIP_SMOKE_GATE=1` — use it only for docs-only PRs.
