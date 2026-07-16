import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from alfred_code.agent_security import (
    AgentSecurityError,
    LaneManifest,
    REVIEW_RESULT,
    WORKER_RESULT,
    _codex_legacy_sandbox_conflict,
    claude_settings,
    codex_profile,
    guard_reason,
    path_allowed,
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
        profile = codex_profile(self.manifest)
        parsed = tomllib.loads(profile)
        filesystem = parsed["permissions"]["alfred_scoped"]["filesystem"][":workspace_roots"]
        self.assertEqual(parsed["approval_policy"], "never")
        self.assertEqual(filesystem["."], "read")
        self.assertEqual(filesystem["packages/learn"], "write")
        self.assertEqual(filesystem["docs/result.md"], "write")
        self.assertEqual(filesystem[WORKER_RESULT], "write")
        self.assertEqual(parsed["permissions"]["alfred_scoped"]["network"]["enabled"], False)
        self.assertEqual(parsed["shell_environment_policy"]["inherit"], "core")
        self.assertFalse(parsed["shell_environment_policy"]["ignore_default_excludes"])
        self.assertEqual(
            parsed["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            str(Path.home() / ".claude/bin/alfred-code-agent-guard"),
        )
        self.assertNotIn("danger-full-access", profile)

    def test_claude_policy_denies_home_reads_and_requires_native_sandbox(self):
        settings = claude_settings(self.manifest, Path("/guard"))
        self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertEqual(settings["sandbox"]["filesystem"]["denyRead"], ["~/"])
        self.assertEqual(settings["sandbox"]["filesystem"]["allowRead"], [str(self.workspace)])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])

    def test_lane_paths_and_control_marker_are_enforced(self):
        self.assertTrue(path_allowed("packages/learn/src/model.py", self.manifest))
        self.assertTrue(path_allowed("docs/result.md", self.manifest))
        self.assertTrue(path_allowed(WORKER_RESULT, self.manifest))
        self.assertFalse(path_allowed("packages/ctrl/src/server.ts", self.manifest))
        self.assertFalse(path_allowed(REVIEW_RESULT, self.manifest))
        self.assertFalse(path_allowed(".git/config", self.manifest))

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
            outside_read = guard_reason(
                {"tool_name": "Read", "tool_input": {"file_path": "/Users/ssd/.ssh/config"}}
            )
        self.assertIn("outside approved", outside)
        self.assertIn("destructive", destructive)
        self.assertIn("limited to read-only", hidden_write)
        self.assertIn("outside the assigned", outside_read)
        self.assertIsNone(allowed)
        self.assertIsNone(verification)
        self.assertIsNone(inspection)

    def test_config_rejects_builtin_superset_presets(self):
        path = self.workspace / "controller.toml"
        path.write_text('[superset]\nworker_agent = "Codex"\nreviewer_agent = "Claude"\n')
        with self.assertRaises(ConfigurationError):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
