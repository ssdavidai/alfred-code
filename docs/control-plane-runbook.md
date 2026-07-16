# Alfred Code v2 deployment and operations runbook

## Install without enabling execution

Run `./install-controller.sh`. It symlinks the v2 CLI and daemon wrapper, creates a mode-0600 configuration with `apply = false`, initializes the SQLite database, and renders a launchd plist. It deliberately does not load launchd.

Run `alfred-code agents-provision`, then `alfred-code doctor`. Provisioning creates the two Alfred-only scoped Superset presets and replaces any YOLO arguments on the built-in Claude and Codex fallbacks with sandboxed settings. A healthy result proves the target repository, live lane policy, database, GitHub repository access, Superset runtime, scoped preset definitions, launcher, guard, and Codex permission-profile compatibility are all reachable. GitHub Projects requires the additional `project` token scope. Superset may use its persisted OAuth login or a `superset-api-key` in the Keychain broker.

Run `alfred-code project-setup` after GitHub has project scope. Put the returned number into `github.project_number` in the controller TOML, then run `doctor` again.

Slack is optional. Store the incoming webhook as `alfred-code-slack-webhook`, set `[slack].enabled = true`, and keep decision-making in GitHub. The Slack feed links back to the issue or PR and is never parsed for approvals.

Import historical evidence with `alfred-code migrate-legacy`. This does not move, delete, rename, or trust the old state files.

Inspect every existing worktree with `alfred-code worktrees-audit`. This command is read-only. Resolve any worktree/branch collision before enabling execution; the controller refuses to overwrite one.

Load the safe service with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ssdavidai.alfred-code-controller-v2.plist`. While `apply = false`, it only reconciles observations. Inspect `~/.alfred-code-state-v2/controller.jsonl`, `launchd.stdout.log`, and `launchd.stderr.log`.

Only after diagnostics and a read-only cycle are clean should `apply = true` be set. Set `github.auto_intake = true` when every open issue should be enrolled automatically; otherwise the configured intake label remains the gate. The next cycle moves every enrolled issue through `Specifying` and posts its plan. Nothing builds until the exact GitHub approval command is present.

## Normal operation

Create an issue. With automatic intake enabled, no label or board movement is required. The controller comments a plan. Read the lane decomposition, affected contracts, verification, base SHA, and risk. Approve or reject by copying the corresponding exact full-hash command from the comment; do not abbreviate it. Leave any other comment when the specification needs revision—the controller incorporates that operator feedback and publishes a fresh hash-bound plan.

Watch the GitHub Project for the portfolio view. Open the linked Superset workspace when you want to see an agent's terminal. Use the PR for code, CI, smoke evidence, and the final merge decision. Use Slack as an inbox only.

`alfred-code status` prints current durable state, leases, and the last hundred events. `alfred-code run-once --dry-run` forces a safe observation even if the config enables apply. `alfred-code worktrees-audit` explains every target-repository worktree with dirty count and PR status.

## Incident rules

If GitHub or Superset is unavailable, do not edit SQLite to make the board look right. Restore authority access and run one reconciliation cycle. The controller will repair its projection from live objects.

If an issue changes during a build, the issue becomes blocked. Decide whether to restore the approved body or file a successor issue. Do not reuse the old approval for the new scope.

If a PR closes without merge, the job becomes quarantined and releases its lane. The workspace and branch remain for inspection. Nothing is deleted automatically with the default configuration.

If a reviewer fails, fix the same PR with a new commit. The old SHA-bound verdict cannot approve the new HEAD. Green CI causes a new independent review workspace.

If `doctor` reports scoped-agent drift, do not launch work from a built-in Superset preset. Run `alfred-code agents-provision` and restart the controller. A scoped agent never owns Git delivery: it may edit only approved lane paths and write its result marker. A changed `.lane`, an agent-created commit, an out-of-scope path, any deletion, a reviewer edit, or a review SHA mismatch quarantines the job and preserves the workspace for inspection.

If launchd misbehaves, `launchctl print gui/$(id -u)/com.ssdavidai.alfred-code-controller-v2` and the two launchd logs are the first evidence. The singleton lock prevents duplicate schedulers. Stopping this new service does not touch the legacy stopped schedule.
