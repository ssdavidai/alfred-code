from pathlib import Path
import tomllib
import unittest

from alfred_code.config import validate_planner_profile


class InstallationTests(unittest.TestCase):
    def test_launchd_prefers_user_local_cli_before_agent_shims(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / "com.ssdavidai.alfred-code-controller-v2.plist.template").read_text()
        path = (
            "__HOME__/.local/bin:__HOME__/.claude/bin:__HOME__/.superset/bin:"
            "/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:"
            "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        )
        self.assertIn(f"<string>{path}</string>", template)
        self.assertLess(path.index("__HOME__/.local/bin"), path.index("__HOME__/.claude/bin"))
        self.assertLess(path.index("/opt/homebrew/opt/node@22/bin"), path.index("/opt/homebrew/bin"))

    def test_scoped_launchers_require_python_311_instead_of_macos_python39(self):
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "bin" / "alfred-code-agent").read_text()
        guard = (root / "bin" / "alfred-code-agent-guard").read_text()
        npm_shell = (root / "bin" / "alfred-code-npm-shell").read_text()
        for script in (launcher, guard):
            self.assertIn("sys.version_info >= (3, 11)", script)
            self.assertIn("/opt/homebrew/bin/python3", script)
            self.assertNotIn("exec /usr/bin/python3", script)
        self.assertIn("/^LAUNCH_REVISION = [0-9]+$/", launcher)
        self.assertNotIn('"revision":3', launcher)
        self.assertNotIn('write_bootstrap_failure "$status"', launcher)
        self.assertIn("node_modules/.bin", npm_shell)
        self.assertIn("! -d", npm_shell)
        self.assertIn('/opt/homebrew/opt/node@22/bin', npm_shell)
        installer = (root / "install-controller.sh").read_text()
        self.assertIn('ln -sf "$ROOT/bin/alfred-code-npm-shell"', installer)

    def test_planner_profile_is_workspace_read_only_and_installed(self):
        root = Path(__file__).resolve().parents[1]
        profile_text = (root / "config/codex-planner.config.toml").read_text()
        profile = tomllib.loads(profile_text)
        policy = profile["permissions"]["alfred_planner"]
        filesystem = policy["filesystem"]
        workspace = filesystem[":workspace_roots"]

        self.assertEqual(profile["default_permissions"], "alfred_planner")
        self.assertEqual(profile["approval_policy"], "never")
        self.assertEqual(filesystem[":minimal"], "read")
        self.assertEqual(workspace["."], "read")
        self.assertEqual(workspace["**/.env"], "deny")
        self.assertFalse(policy["network"]["enabled"])
        self.assertNotIn('= "write"', profile_text)
        installer = (root / "install-controller.sh").read_text()
        self.assertIn("config/codex-planner.config.toml", installer)
        self.assertIn('install -m 600', installer)
        validated = validate_planner_profile(root / "config/codex-planner.config.toml")
        self.assertEqual(validated["workspace"], "read-only")
        self.assertEqual(validated["network"], "disabled")


if __name__ == "__main__":
    unittest.main()
