from pathlib import Path
import unittest


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
        self.assertIn("node_modules/.bin", npm_shell)
        self.assertIn("! -d", npm_shell)
        installer = (root / "install-controller.sh").read_text()
        self.assertIn('ln -sf "$ROOT/bin/alfred-code-npm-shell"', installer)


if __name__ == "__main__":
    unittest.main()
