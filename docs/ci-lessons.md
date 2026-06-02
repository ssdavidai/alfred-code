# CI lessons ledger (compound engineering)

This file is the **flywheel**: every time a CI check fails and gets root-caused,
the fix appends one line here. Every future lane reads it *before* writing code
(see `agents/lane-worker.md`), so each failure becomes a guardrail that
pre-empts the next occurrence instead of repeating it.

**Format (append, newest at the bottom of the relevant section):**

```
- YYYY-MM-DD · <check that failed> · <root cause> · <the rule that prevents it>
```

**Who appends:**
- `bin/alfred-code-fix-pr` agents — mandatory on any real (non-flake) fix.
- Lane / orchestrator agents — when they trip and fix a CI break mid-build.
- Operators — when they hand-fix CI and want it to stick.

Keep entries one line. If a class of failure recurs, promote it into the
**Wasp `Promise<T>` trap**-style gotchas in `agents/lane-worker.md` so it's in
every agent's inherited persona, and reference it here.

---

## Documented flakes (NOT failures — bypassable, never "fix" these)

- `compose-lint` — pre-existing tailscale-profile-gating flake; passes on main from the same parent commit. Admin-bypass after smoke is green.
- `test-voice-bridge` — pre-existing slow-stall flake; safe to bypass when voice-bridge code is unchanged.

## Real lessons

- 2026-05-29 · build-ctrl (Wasp) · Wasp `Payload` generic rejects a concrete `Promise<MyResp>` return type · declare ops `async (args, context): Promise<any>` — Wasp reads `main.wasp` for the wire shape, the TS type is a local hint only. (See lane-worker.md.)
- 2026-05-29 · ctrl-api migrations · `user_version` drift between branches collided on the same migration number · never edit a merged migration; append a new numbered file.
- 2026-05-29 · test glob · `test_*.ts` glob escaped the intended dir and pulled stray fixtures · anchor test globs to the package dir.
- 2026-05-28 · GitGuardian · test fixtures embedding realistic-looking `tskey-auth-…` tokens tripped secret scanning · use obviously-fake sentinel tokens in fixtures.
- 2026-06-02 · node:test (ctrl) · importing phone.ts in a test failed with ENOENT (mkdirSync side effect) · avoid importing route modules directly in tests; test the DB helper logic inline or via the full HTTP server mock.
- 2026-06-02 · compose-lint (tailscale gate) · `docker compose --profile tailscale config --services | grep` is version-fragile — whether profile-gated services appear in `--services` output varies by the runner's docker-compose version, so it false-reds on PRs that never touched compose · assert the contract deterministically via `docker compose config --format json | jq '.services.tailscale.profiles | index("tailscale")'`, never the `--profile … --services` dance. (alfred PR #230)
- 2026-06-02 · test-voice-bridge · ran on every PR and slow-stall-failed on ones that don't touch voice-bridge — a recurring false red the loop hand-waved · path-gate the job (real work only when `packages/voice-bridge/**` changes; green-skip otherwise) + `timeout-minutes`. GENERAL RULE: a check is only a bypassable "flake" when the PR doesn't touch that check's area AND it's on the documented list — never bypass by name alone; the durable fix is to make the check not fire/fail on unrelated PRs. (alfred PR #230)
