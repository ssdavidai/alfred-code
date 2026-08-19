import subprocess
import tempfile
import unittest
from pathlib import Path

from alfred_code.errors import AuthorityUnavailable
from alfred_code.source import (
    sync_default_branch_checkout,
    verify_default_branch_checkout,
)


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class SourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "origin.git"
        self.author = self.root / "author"
        self.checkout = self.root / "controller"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(
            ["git", "init", "-b", "main", str(self.author)], check=True, capture_output=True
        )
        git(self.author, "config", "user.email", "test@example.com")
        git(self.author, "config", "user.name", "Test")
        (self.author / "source.txt").write_text("one\n")
        git(self.author, "add", "source.txt")
        git(self.author, "commit", "-m", "one")
        git(self.author, "remote", "add", "origin", str(self.remote))
        git(self.author, "push", "-u", "origin", "main")
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.remote), str(self.checkout)],
            check=True,
            capture_output=True,
        )
        git(self.checkout, "switch", "--detach")
        self.first_sha = git(self.checkout, "rev-parse", "HEAD")

    def tearDown(self):
        self.temporary.cleanup()

    def advance_remote(self) -> str:
        (self.author / "source.txt").write_text("two\n")
        git(self.author, "add", "source.txt")
        git(self.author, "commit", "-m", "two")
        git(self.author, "push", "origin", "main")
        return git(self.author, "rev-parse", "HEAD")

    def test_sync_advances_clean_detached_checkout_to_remote_main(self):
        second_sha = self.advance_remote()

        result = sync_default_branch_checkout(self.checkout)

        self.assertEqual(result.before_sha, self.first_sha)
        self.assertEqual(result.head_sha, second_sha)
        self.assertTrue(result.changed)
        self.assertEqual((self.checkout / "source.txt").read_text(), "two\n")
        self.assertEqual(git(self.checkout, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        verify_default_branch_checkout(self.checkout, second_sha)

    def test_sync_is_idempotent_when_remote_has_not_advanced(self):
        result = sync_default_branch_checkout(self.checkout)

        self.assertEqual(result.head_sha, self.first_sha)
        self.assertFalse(result.changed)
        verify_default_branch_checkout(self.checkout, self.first_sha)

    def test_sync_refuses_dirty_checkout_without_discarding_work(self):
        second_sha = self.advance_remote()
        (self.checkout / "local.txt").write_text("do not delete\n")

        with self.assertRaisesRegex(AuthorityUnavailable, "dirty"):
            sync_default_branch_checkout(self.checkout)

        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), self.first_sha)
        self.assertEqual((self.checkout / "local.txt").read_text(), "do not delete\n")
        self.assertNotEqual(second_sha, self.first_sha)

    def test_sync_refuses_to_move_an_attached_branch(self):
        git(self.checkout, "switch", "main")
        self.advance_remote()

        with self.assertRaisesRegex(AuthorityUnavailable, "dedicated detached checkout"):
            sync_default_branch_checkout(self.checkout)

        self.assertEqual(git(self.checkout, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_planner_verification_rejects_a_stale_checkout(self):
        second_sha = self.advance_remote()
        git(self.checkout, "fetch", "--no-tags", "origin", "main")

        with self.assertRaisesRegex(AuthorityUnavailable, "disagree"):
            verify_default_branch_checkout(self.checkout, second_sha)

        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), self.first_sha)


if __name__ == "__main__":
    unittest.main()
