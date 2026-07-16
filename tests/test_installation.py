from pathlib import Path
import unittest


class InstallationTests(unittest.TestCase):
    def test_launchd_prefers_user_local_cli_before_agent_shims(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "launchd" / "com.ssdavidai.alfred-code-controller-v2.plist.template").read_text()
        path = (
            "__HOME__/.local/bin:__HOME__/.claude/bin:__HOME__/.superset/bin:"
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        )
        self.assertIn(f"<string>{path}</string>", template)
        self.assertLess(path.index("__HOME__/.local/bin"), path.index("__HOME__/.claude/bin"))


if __name__ == "__main__":
    unittest.main()
