import json
import os
import shutil
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from alfred_code.agent_security import (
    AgentSecurityError,
    LAUNCH_REVISION,
    LAUNCH_STATUS,
    LaneManifest,
    REVIEW_RESULT,
    WORKER_RESULT,
    _codex_isolation_arguments,
    _codex_legacy_sandbox_conflict,
    _normalized_git_origin,
    build_provider_command,
    claude_settings,
    codex_profile,
    guard_reason,
    launch,
    main,
    path_allowed,
    prepare_dependency_overlay,
    runtime_cache_environment,
    validate_provider_arguments,
)
from alfred_code.config import load_config
from alfred_code.errors import ConfigurationError


class AgentSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / ".git").write_text("gitdir: /tmp/example")
        (self.workspace / ".lane").write_text(
            json.dumps(
                {
                    "lane": "II",
                    "issue": 299,
                    "allowed": ["packages/learn/**", "docs/result.md"],
                    "verify": "cd packages/learn && python3 -m pytest -q",
                    "controller_job": "learn-299",
                    "role": "worker",
                    "security_policy": "alfred-scoped-v1",
                }
            )
        )
        self.manifest = LaneManifest.load(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def prepare_fake_codex_runtime(self):
        (self.workspace / ".codex").mkdir(exist_ok=True)
        guard = self.workspace / ".claude/bin/alfred-code-agent-guard"
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_text("#!/bin/sh\n")
        guard.chmod(0o700)
        npm_shell = self.workspace / ".claude/bin/alfred-code-npm-shell"
        npm_shell.write_text("#!/bin/sh\n")
        npm_shell.chmod(0o700)

    def test_yolo_and_policy_override_arguments_are_rejected(self):
        unsafe = [
            ["--dangerously-bypass-approvals-and-sandbox"],
            ["--dangerously-skip-permissions"],
            ["--permission-mode", "bypassPermissions"],
            ["--ask-for-approval=never"],
            ["--sandbox=danger-full-access"],
            ["--add-dir", "/Users/ssd"],
            ["-c", "sandbox_mode=\"danger-full-access\""],
        ]
        for arguments in unsafe:
            with self.subTest(arguments=arguments), self.assertRaises(AgentSecurityError):
                validate_provider_arguments("codex", arguments)

    def test_legacy_codex_sandbox_cannot_override_scoped_profile(self):
        config = self.workspace / "codex-config.toml"
        config.write_text('sandbox_mode = "workspace-write"\n')
        self.assertIn("override named permission profiles", _codex_legacy_sandbox_conflict(config))

    def test_codex_profile_is_lane_scoped_read_only_elsewhere_and_offline(self):
        profile_path = self.workspace / "alfred-scoped-test.config.toml"
        dependency_target = self.workspace.parent / f"{self.workspace.name}-node-modules"
        dependency_target.mkdir(exist_ok=True)
        self.addCleanup(dependency_target.rmdir)
        dependency_link = self.workspace / "packages/learn/node_modules"
        dependency_link.parent.mkdir(parents=True)
        dependency_link.symlink_to(dependency_target, target_is_directory=True)
        profile = codex_profile(
            self.manifest,
            profile_path=profile_path,
            hook_trust_hash="sha256:test",
            toolchain_paths=(Path("/trusted/bin"),),
            git_metadata_paths=(Path("/trusted/git"),),
        )
        parsed = tomllib.loads(profile)
        filesystem = parsed["permissions"]["alfred_scoped"]["filesystem"][":workspace_roots"]
        self.assertEqual(parsed["approval_policy"], "never")
        self.assertEqual(filesystem["."], "read")
        self.assertEqual(filesystem["packages/learn"], "write")
        self.assertEqual(filesystem["docs/result.md"], "write")
        self.assertEqual(filesystem[WORKER_RESULT], "write")
        self.assertNotIn("**/.env.*", filesystem)
        self.assertEqual(filesystem["**/.env.production"], "deny")
        self.assertEqual(parsed["permissions"]["alfred_scoped"]["network"]["enabled"], False)
        self.assertEqual(parsed["permissions"]["alfred_scoped"]["filesystem"][":tmpdir"], "write")
        self.assertEqual(parsed["permissions"]["alfred_scoped"]["filesystem"]["/trusted/bin"], "read")
        self.assertEqual(parsed["permissions"]["alfred_scoped"]["filesystem"]["/trusted/git"], "read")
        self.assertEqual(
            parsed["permissions"]["alfred_scoped"]["filesystem"][str(dependency_target.resolve())],
            "read",
        )
        self.assertEqual(filesystem["packages/learn/dist"], "write")
        self.assertNotIn("packages/learn/.cache", filesystem)
        self.assertEqual(parsed["shell_environment_policy"]["inherit"], "core")
        self.assertFalse(parsed["shell_environment_policy"]["ignore_default_excludes"])
        self.assertTrue(parsed["shell_environment_policy"]["set"]["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual(parsed["shell_environment_policy"]["set"]["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(parsed["shell_environment_policy"]["set"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(
            parsed["shell_environment_policy"]["set"]["npm_config_scripts_prepend_node_path"],
            "false",
        )
        self.assertEqual(
            parsed["shell_environment_policy"]["set"]["npm_config_script_shell"],
            str((Path.home() / ".claude/bin/alfred-code-npm-shell").resolve()),
        )
        cache_environment = runtime_cache_environment(self.manifest)
        self.assertEqual(
            parsed["shell_environment_policy"]["set"]["npm_config_cache"],
            cache_environment["npm_config_cache"],
        )
        self.assertEqual(
            parsed["shell_environment_policy"]["set"]["XDG_CACHE_HOME"],
            cache_environment["XDG_CACHE_HOME"],
        )
        self.assertFalse(
            Path(cache_environment["npm_config_cache"]).is_relative_to(self.workspace)
        )
        self.assertEqual(
            parsed["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            str(Path.home() / ".claude/bin/alfred-code-agent-guard"),
        )
        trust_key = f"{profile_path.resolve()}:pre_tool_use:0:0"
        self.assertEqual(parsed["hooks"]["state"][trust_key]["trusted_hash"], "sha256:test")
        self.assertNotIn("danger-full-access", profile)

    def test_codex_integrations_are_replaced_as_whole_tables(self):
        arguments = _codex_isolation_arguments()
        self.assertIn("mcp_servers={}", arguments)
        self.assertIn("plugins={}", arguments)
        self.assertIn("features={hooks=true}", arguments)
        self.assertIn("memories={generate_memories=false,use_memories=false}", arguments)
        self.assertFalse(any('mcp_servers."' in value for value in arguments))

    def test_equivalent_github_origin_forms_match_for_dependency_reuse(self):
        self.assertEqual(
            _normalized_git_origin("https://github.com/ssdavidai/alfred.git"),
            _normalized_git_origin("git@github.com:ssdavidai/alfred"),
        )

    def test_broken_package_dependency_link_gets_non_overwriting_root_overlay(self):
        package_root = self.workspace / "packages/learn"
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "package.json").write_text(
            json.dumps({"dependencies": {"tsx": "1.0.0"}, "devDependencies": {"esbuild": "1.0.0"}})
        )
        broken = package_root / "node_modules"
        broken.symlink_to(broken, target_is_directory=True)
        dependency_target = self.workspace / ".dependency-cache"
        (dependency_target / "tsx").mkdir(parents=True)
        (dependency_target / "esbuild").mkdir()

        with patch.dict(os.environ, {"ALFRED_CODE_NODE_MODULES": str(dependency_target)}):
            overlay = prepare_dependency_overlay(self.manifest)

        self.assertEqual(overlay, self.workspace / "node_modules")
        self.assertTrue(overlay.is_symlink())
        self.assertEqual(overlay.resolve(), dependency_target.resolve())

    def test_dependency_overlay_reuses_the_primary_checkout_offline(self):
        package_root = self.workspace / "packages/learn"
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "package.json").write_text(
            json.dumps({"dependencies": {"tsx": "1.0.0"}, "devDependencies": {"esbuild": "1.0.0"}})
        )

        source_checkout = self.workspace.parent / f"{self.workspace.name}-source"
        source_git = source_checkout / ".git"
        source_modules = source_checkout / "packages/learn/node_modules"
        source_git.mkdir(parents=True)
        (source_modules / "tsx").mkdir(parents=True)
        (source_modules / "esbuild").mkdir()
        self.addCleanup(shutil.rmtree, source_checkout, True)

        with (
            patch("alfred_code.agent_security._git_metadata_paths", return_value=(source_git,)),
            patch(
                "alfred_code.agent_security._git_origin_url",
                return_value="https://github.com/ssdavidai/alfred",
            ),
            patch.dict(os.environ, {"ALFRED_CODE_NODE_MODULES": ""}),
        ):
            overlay = prepare_dependency_overlay(self.manifest)

        self.assertEqual(overlay, self.workspace / "node_modules")
        self.assertEqual(overlay.resolve(), source_modules.resolve())

    def test_dependency_overlay_never_replaces_an_existing_path(self):
        overlay = self.workspace / "node_modules"
        overlay.write_text("keep")
        with patch.dict(os.environ, {"ALFRED_CODE_NODE_MODULES": "/tmp/dependencies"}):
            self.assertIsNone(prepare_dependency_overlay(self.manifest))
        self.assertEqual(overlay.read_text(), "keep")

    def test_codex_uses_unattended_exec_for_the_exact_worktree(self):
        with (
            patch("alfred_code.agent_security._provider_binary", return_value="/bin/codex"),
            patch("alfred_code.agent_security._codex_legacy_sandbox_conflict", return_value=None),
        ):
            command = build_provider_command(
                "codex",
                ["--", "build it"],
                self.manifest,
                profile_name="alfred-scoped-test",
            )
        self.assertIn("exec", command)
        self.assertLess(command.index("exec"), command.index("--"))
        project_override = command[command.index("exec") - 1]
        self.assertIn(str(self.workspace), project_override)
        self.assertIn('trust_level="trusted"', project_override)

    def test_claude_policy_denies_home_reads_and_requires_native_sandbox(self):
        settings = claude_settings(self.manifest, Path("/guard"))
        self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertEqual(settings["sandbox"]["filesystem"]["denyRead"], ["~/"])
        self.assertEqual(
            settings["sandbox"]["filesystem"]["allowRead"],
            [
                str(self.workspace),
                str((Path.home() / ".claude/bin/alfred-code-npm-shell").resolve()),
            ],
        )
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])

    def test_claude_can_read_only_a_validated_dependency_overlay_target(self):
        target = self.workspace / ".dependency-cache"
        target.mkdir()
        overlay = self.workspace / "node_modules"
        overlay.symlink_to(target, target_is_directory=True)
        settings = claude_settings(self.manifest, Path("/guard"))
        self.assertEqual(
            settings["sandbox"]["filesystem"]["allowRead"],
            [
                str(self.workspace),
                str(target.resolve()),
                str((Path.home() / ".claude/bin/alfred-code-npm-shell").resolve()),
            ],
        )

    def test_lane_paths_and_control_marker_are_enforced(self):
        self.assertTrue(path_allowed("packages/learn/src/model.py", self.manifest))
        self.assertTrue(path_allowed("docs/result.md", self.manifest))
        self.assertTrue(path_allowed(WORKER_RESULT, self.manifest))
        self.assertFalse(path_allowed("packages/ctrl/src/server.ts", self.manifest))
        self.assertFalse(path_allowed(REVIEW_RESULT, self.manifest))
        self.assertFalse(path_allowed(LAUNCH_STATUS, self.manifest))
        self.assertFalse(path_allowed(".git/config", self.manifest))

    def test_directory_allow_rule_covers_descendants(self):
        value = json.loads((self.workspace / ".lane").read_text())
        value["allowed"] = ["packages/learn/tests/"]
        (self.workspace / ".lane").write_text(json.dumps(value))
        manifest = LaneManifest.load(self.workspace)
        self.assertTrue(path_allowed("packages/learn/tests/test_worker.py", manifest))
        self.assertFalse(path_allowed("packages/learn/src/worker.py", manifest))

    def test_self_check_reports_supported_runtime_and_policy(self):
        output = StringIO()
        with redirect_stdout(output):
            result = main(["--self-check"])
        value = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(value["ok"])
        self.assertGreaterEqual(tuple(value["python"][:2]), (3, 11))
        self.assertEqual(value["policy"], "alfred-scoped-v1")

    def test_provider_self_check_executes_the_resolved_binary(self):
        provider = self.workspace / "fake-provider"
        provider.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n")
        provider.chmod(0o700)
        output = StringIO()
        with (
            patch("alfred_code.agent_security._provider_binary", return_value=str(provider)),
            redirect_stdout(output),
        ):
            result = main(["--self-check", "codex"])
        value = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(value["provider"], "codex")
        self.assertEqual(value["provider_binary"], str(provider))
        self.assertEqual(value["provider_version"], "--version")

    def test_provider_exit_is_persisted_before_controller_reconciliation(self):
        (self.workspace / ".codex").mkdir()
        with (
            patch("alfred_code.agent_security.workspace_from_environment", return_value=self.workspace),
            patch.object(Path, "home", return_value=self.workspace),
            patch("alfred_code.agent_security._provider_binary", return_value="/bin/codex"),
            patch("alfred_code.agent_security.codex_hook_trust_hash", return_value="sha256:test"),
            patch("alfred_code.agent_security.build_provider_command", return_value=["codex"]),
            patch("alfred_code.agent_security.subprocess.call", return_value=70),
        ):
            guard = self.workspace / ".claude/bin/alfred-code-agent-guard"
            guard.parent.mkdir(parents=True)
            guard.write_text("#!/bin/sh\n")
            guard.chmod(0o700)
            npm_shell = self.workspace / ".claude/bin/alfred-code-npm-shell"
            npm_shell.write_text("#!/bin/sh\n")
            npm_shell.chmod(0o700)
            result = launch("codex", [])

        marker = json.loads((self.workspace / LAUNCH_STATUS).read_text())
        self.assertEqual(result, 70)
        self.assertEqual(marker["status"], "exited")
        self.assertEqual(marker["exit_code"], 70)
        self.assertEqual(marker["controller_job"], "learn-299")

    def test_launch_redirects_tool_caches_outside_the_lane_workspace(self):
        self.prepare_fake_codex_runtime()
        runtime_temp = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_temp.cleanup)
        captured_environment = {}

        def capture_launch(command, cwd, env):
            captured_environment.update(env)
            return 0

        with (
            patch("alfred_code.agent_security.workspace_from_environment", return_value=self.workspace),
            patch.object(Path, "home", return_value=self.workspace),
            patch("alfred_code.agent_security.tempfile.gettempdir", return_value=runtime_temp.name),
            patch("alfred_code.agent_security._provider_binary", return_value="/bin/codex"),
            patch("alfred_code.agent_security.codex_hook_trust_hash", return_value="sha256:test"),
            patch("alfred_code.agent_security.build_provider_command", return_value=["codex"]),
            patch("alfred_code.agent_security.subprocess.call", side_effect=capture_launch),
        ):
            launch("codex", [])

        for name in ("XDG_CACHE_HOME", "npm_config_cache"):
            cache_path = Path(captured_environment[name])
            self.assertTrue(cache_path.is_dir())
            self.assertTrue(cache_path.is_relative_to(Path(runtime_temp.name)))
            self.assertFalse(cache_path.is_relative_to(self.workspace))

    def test_repair_launch_overwrites_stale_result_and_requires_exact_binding(self):
        lane = json.loads((self.workspace / ".lane").read_text())
        lane.update(
            {
                "mode": "repair",
                "head_sha": "a" * 40,
                "handoff_token": "b" * 48,
                "attempt": 1,
            }
        )
        (self.workspace / ".lane").write_text(json.dumps(lane))
        (self.workspace / WORKER_RESULT).write_text(
            json.dumps({"status": "ready", "summary": "stale"})
        )
        self.prepare_fake_codex_runtime()
        with (
            patch("alfred_code.agent_security.workspace_from_environment", return_value=self.workspace),
            patch.object(Path, "home", return_value=self.workspace),
            patch("alfred_code.agent_security._provider_binary", return_value="/bin/codex"),
            patch("alfred_code.agent_security.codex_hook_trust_hash", return_value="sha256:test"),
            patch("alfred_code.agent_security.build_provider_command", return_value=["codex"]),
            patch("alfred_code.agent_security.subprocess.call", return_value=0),
        ):
            launch("codex", [])

        result = json.loads((self.workspace / WORKER_RESULT).read_text())
        marker = json.loads((self.workspace / LAUNCH_STATUS).read_text())
        self.assertEqual(result["status"], "retrying")
        self.assertEqual(result["handoff_token"], "b" * 48)
        self.assertEqual(marker["status"], "exited")

    def test_repair_launch_accepts_only_token_and_sha_bound_handoff(self):
        lane = json.loads((self.workspace / ".lane").read_text())
        lane.update(
            {
                "mode": "repair",
                "head_sha": "c" * 40,
                "handoff_token": "d" * 48,
                "attempt": 2,
            }
        )
        (self.workspace / ".lane").write_text(json.dumps(lane))
        self.prepare_fake_codex_runtime()

        def write_bound_result(command, cwd, env):
            (self.workspace / WORKER_RESULT).write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "summary": "fixed",
                        "head_sha": "c" * 40,
                        "handoff_token": "d" * 48,
                        "attempt": 2,
                    }
                )
            )
            return 0

        with (
            patch("alfred_code.agent_security.workspace_from_environment", return_value=self.workspace),
            patch.object(Path, "home", return_value=self.workspace),
            patch("alfred_code.agent_security._provider_binary", return_value="/bin/codex"),
            patch("alfred_code.agent_security.codex_hook_trust_hash", return_value="sha256:test"),
            patch("alfred_code.agent_security.build_provider_command", return_value=["codex"]),
            patch("alfred_code.agent_security.subprocess.call", side_effect=write_bound_result),
        ):
            launch("codex", [])

        marker = json.loads((self.workspace / LAUNCH_STATUS).read_text())
        self.assertEqual(marker["status"], "completed")
        self.assertEqual(marker["attempt"], 2)
        self.assertEqual(marker["revision"], LAUNCH_REVISION)

    def test_reviewer_verdict_is_a_valid_launch_handoff(self):
        lane = json.loads((self.workspace / ".lane").read_text())
        lane.update({"allowed": [], "role": "reviewer"})
        (self.workspace / ".lane").write_text(json.dumps(lane))
        (self.workspace / REVIEW_RESULT).write_text(
            json.dumps({"head_sha": "a" * 40, "verdict": "fail", "findings": "bug"})
        )
        (self.workspace / ".codex").mkdir()
        with (
            patch("alfred_code.agent_security.workspace_from_environment", return_value=self.workspace),
            patch.object(Path, "home", return_value=self.workspace),
            patch("alfred_code.agent_security._provider_binary", return_value="/bin/codex"),
            patch("alfred_code.agent_security.codex_hook_trust_hash", return_value="sha256:test"),
            patch("alfred_code.agent_security.build_provider_command", return_value=["codex"]),
            patch("alfred_code.agent_security.subprocess.call", return_value=0),
        ):
            guard = self.workspace / ".claude/bin/alfred-code-agent-guard"
            guard.parent.mkdir(parents=True)
            guard.write_text("#!/bin/sh\n")
            guard.chmod(0o700)
            npm_shell = self.workspace / ".claude/bin/alfred-code-npm-shell"
            npm_shell.write_text("#!/bin/sh\n")
            npm_shell.chmod(0o700)
            result = launch("codex", [])

        marker = json.loads((self.workspace / LAUNCH_STATUS).read_text())
        self.assertEqual(result, 0)
        self.assertEqual(marker["status"], "completed")
        self.assertIn("valid result marker", marker["reason"])

    def test_hook_blocks_out_of_lane_edits_and_destructive_shell(self):
        with patch.dict(os.environ, {"SUPERSET_WORKSPACE_PATH": str(self.workspace)}):
            outside = guard_reason(
                {"tool_name": "Edit", "tool_input": {"file_path": "packages/ctrl/src/server.ts"}}
            )
            destructive = guard_reason(
                {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD^"}}
            )
            allowed = guard_reason(
                {"tool_name": "Edit", "tool_input": {"file_path": "packages/learn/src/model.py"}}
            )
            hidden_write = guard_reason(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "python3 -c 'open(\"outside.txt\", \"w\").write(\"x\")'"},
                }
            )
            verification = guard_reason(
                {"tool_name": "Bash", "tool_input": {"command": self.manifest.verify}}
            )
            inspection = guard_reason(
                {"tool_name": "Bash", "tool_input": {"command": "git diff --check"}}
            )
            safe_redirection = guard_reason(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git status 2>&1; node --version 2>/dev/stderr"},
                }
            )
            outside_read = guard_reason(
                {"tool_name": "Read", "tool_input": {"file_path": "/Users/ssd/.ssh/config"}}
            )
            current_codex_patch_payload = guard_reason(
                {
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "patch_text": (
                            "*** Begin Patch\n"
                            "*** Update File: packages/learn/src/model.py\n"
                            "@@\n-old\n+new\n"
                            "*** End Patch"
                        )
                    },
                }
            )
            outside_codex_patch_payload = guard_reason(
                {
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "patch_text": (
                            "*** Begin Patch\n"
                            "*** Update File: packages/ctrl/src/server.ts\n"
                            "@@\n-old\n+new\n"
                            "*** End Patch"
                        )
                    },
                }
            )
        self.assertIn("outside approved", outside)
        self.assertIn("destructive", destructive)
        self.assertIn("destructive", hidden_write)
        self.assertIn("outside the assigned", outside_read)
        self.assertIsNone(allowed)
        self.assertIsNone(verification)
        self.assertIsNone(inspection)
        self.assertIsNone(safe_redirection)
        self.assertIsNone(current_codex_patch_payload)
        self.assertIn("outside approved", outside_codex_patch_payload)

    def test_config_rejects_builtin_superset_presets(self):
        path = self.workspace / "controller.toml"
        path.write_text('[superset]\nworker_agent = "Codex"\nreviewer_agent = "Claude"\n')
        with self.assertRaises(ConfigurationError):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
