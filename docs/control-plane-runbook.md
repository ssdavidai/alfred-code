# Alfred Code v2 deployment and operations runbook

## Install without enabling execution

Run `./install-controller.sh`. It symlinks the v2 CLI and daemon wrapper, creates a mode-0600 configuration with `apply = false`, initializes the SQLite database, and renders a launchd plist. It deliberately does not load launchd.

Run `alfred-code agents-provision`, then `alfred-code doctor`. Provisioning creates the two Alfred-only scoped Superset presets and replaces any YOLO arguments on the built-in Claude and Codex fallbacks with sandboxed settings. A healthy result proves the target repository, live lane policy, database, GitHub repository access authenticated as the immutable trusted operator `ssdavidai`, Superset runtime, scoped preset definitions, launcher, guard, and Codex permission-profile compatibility are all reachable. GitHub Projects requires the additional `project` token scope. Superset may use its persisted OAuth login or a `superset-api-key` in the Keychain broker.

Run `alfred-code project-setup` after GitHub has project scope. Put the returned number into `github.project_number` in the controller TOML, then run `doctor` again.

Slack is optional. Store the incoming webhook as `alfred-code-slack-webhook`, set `[slack].enabled = true`, and keep decision-making in GitHub. The Slack feed links back to the issue or PR and is never parsed for approvals.

Import historical evidence with `alfred-code migrate-legacy`. This does not move, delete, rename, or trust the old state files.

Inspect every existing worktree with `alfred-code worktrees-audit`. This command is read-only. Resolve any worktree/branch collision before enabling execution; the controller refuses to overwrite one.

Load the safe service with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ssdavidai.alfred-code-controller-v2.plist`. While `apply = false`, it only reconciles observations. Inspect `~/.alfred-code-state-v2/controller.jsonl`, `launchd.stdout.log`, and `launchd.stderr.log`.

Only after diagnostics and a read-only cycle are clean should `apply = true` be set. With a configured Project, every open issue is projected into the agent-silent Backlog; the legacy `auto_intake` and intake-label settings apply only when no Project scheduler is configured. Order candidates in Inbox, drag selected cards into Sprint Queue, and use the dashboard Start Sprint action or `alfred-code sprint-start`. Only active sprint cards move through Specifying and post plans. Nothing builds until the exact GitHub approval command is present.

## Normal operation

Create an issue. With automatic intake enabled, no label or board movement is required. The controller comments a plan. Read the lane decomposition, affected contracts, verification, base SHA, and risk. While logged in as `ssdavidai`, approve or reject by copying the corresponding exact full-hash command from the comment; do not abbreviate it. Leave any other comment from that same account when the specification needs revision—the controller incorporates that trusted operator feedback and publishes a fresh hash-bound plan. Comments from every other account are untrusted data and cannot control the system.

Use a GitHub Project board grouped by Control stage as the draggable product surface. Backlog and Inbox are agent-silent. Sprint Queue is an explicit commitment waiting for Start Sprint. Open the linked Superset workspace when you want to see an agent's terminal. Use the PR for code, CI, smoke evidence, and the final merge decision. Use Slack as an inbox only.

`alfred-code status` prints current durable state, leases, and the last hundred events. `alfred-code run-once --dry-run` forces a safe observation even if the config enables apply. `alfred-code worktrees-audit` explains every target-repository worktree with dirty count and PR status.

## Incident rules

If GitHub or Superset is unavailable, do not edit SQLite to make the board look right. Restore authority access and run one reconciliation cycle. The controller will repair its projection from live objects.

If an issue changes during a build, the issue becomes blocked. Decide whether to restore the approved body or file a successor issue. Do not reuse the old approval for the new scope.

If a PR closes without merge, the job becomes quarantined and releases its lane. The workspace and branch remain for inspection. Nothing is deleted automatically with the default configuration.

If a reviewer fails, the controller sends the exact findings to a scoped worker in the original workspace and repairs the same PR without expanding the approved paths. The old SHA-bound verdict cannot approve the repair commit; green CI causes a new independent review workspace. The repair handoff is bound to the failed SHA and a controller nonce, and automatic attempts stop at `review_repair_max_attempts` (default two). If that cap is exhausted, inspect the preserved PR and workspace; do not raise the cap or bypass scope merely to force progress. A manual in-scope commit to the same PR also changes HEAD and triggers a fresh review.

If `doctor` reports scoped-agent drift, do not launch work from a built-in Superset preset. Run `alfred-code agents-provision` and restart the controller. A scoped agent never owns Git delivery: it may edit only approved lane paths and write its result marker. A changed `.lane`, an agent-created commit, an out-of-scope path, any deletion, a reviewer edit, or a review SHA mismatch quarantines the job and preserves the workspace for inspection.

If the board says `Building` but the terminal has already exited, inspect `.alfred-code-launch.json` in the workspace and the controller log. No marker within `worker_launch_timeout_seconds`, or an `exited`/`failed` marker, blocks and releases the lane with the actual launch reason. A `running` launch marker proves that the wrapper started; the controller does not consume the worker result until the marker becomes `completed`. Do not delete the workspace or edit SQLite; after repairing a pre-handshake launcher failure, the controller may retry that untouched workspace once under the same scoped preset. A newer launcher policy may also resume old in-scope progress after the controller revalidates the complete diff.

If a Codex terminal reports an untrusted hook, missing OpenSSL configuration, unreadable Git worktree metadata, or an unresolved dependency behind a tracked `node_modules` link, do not enable a YOLO or hook-trust-bypass flag. Confirm that the generated `~/.codex/alfred-scoped-*.config.toml` has `approval_policy = "never"`, network disabled, an exact `trusted_hash`, and only the expected read/write grants. The wrapper fails closed when it cannot obtain that hook hash. For a broken tracked dependency link it creates only a non-overwriting root-level resolution overlay to a validated compatible cache; the tracked repository link is left untouched.

If launchd misbehaves, `launchctl print gui/$(id -u)/com.ssdavidai.alfred-code-controller-v2` and the two launchd logs are the first evidence. The singleton lock prevents duplicate schedulers. Stopping this new service does not touch the legacy stopped schedule.
