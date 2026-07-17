import unittest

from alfred_code.config import GitHubConfig
from alfred_code.github import GitHubClient


class FakeGitHub(GitHubClient):
    def __init__(self):
        super().__init__(
            GitHubConfig(
                repo="owner/repo",
                owner="owner",
                approval_command="/approve-plan",
                approvers=("owner",),
                reviewers=("owner",),
            )
        )
        self.comments = []
        self.posts = []

    def issue_comments(self, number):
        return self.comments

    def post_issue_comment(self, number, body):
        self.posts.append((number, body))
        return "https://example/comment"


class GitHubTests(unittest.TestCase):
    def test_cycle_cache_reuses_issue_and_comment_observations(self):
        client = GitHubClient(
            GitHubConfig(
                repo="owner/repo",
                owner="owner",
                approval_command="/approve-plan",
                approvers=("owner",),
                reviewers=("owner",),
            )
        )
        calls = []

        def fake_json(arguments, timeout=120):
            calls.append(tuple(arguments))
            if arguments[:2] == ["issue", "list"]:
                return [{"number": 7, "state": "OPEN", "title": "cached"}]
            if arguments[:2] == ["issue", "view"]:
                return {"number": 7, "state": "OPEN", "title": "fresh"}
            if arguments[0] == "api":
                return [{"id": 1, "body": "hello"}]
            raise AssertionError(arguments)

        client._json = fake_json
        self.assertEqual(client.open_issues()[0]["title"], "cached")
        self.assertEqual(client.issue(7)["title"], "cached")
        self.assertEqual(client.issue_comments(7), client.issue_comments(7))
        self.assertEqual(len(calls), 2)

        client.begin_cycle()
        self.assertEqual(client.issue(7)["title"], "fresh")
        self.assertEqual(len(calls), 3)

    def test_approval_must_be_full_exact_and_from_allowlist(self):
        client = FakeGitHub()
        digest = "a" * 64
        client.comments = [
            {"id": 1, "body": f"/approve-plan {digest[:12]}", "user": {"login": "owner"}},
            {"id": 2, "body": f"/approve-plan {digest}", "user": {"login": "intruder"}},
        ]
        self.assertIsNone(client.find_approval(5, digest))
        client.comments.append(
            {
                "id": 3,
                "body": f"/approve-plan {digest}\n",
                "user": {"login": "owner"},
                "created_at": "now",
                "html_url": "https://example/3",
            }
        )
        approval = client.find_approval(5, digest)
        self.assertEqual(approval["comment_id"], "3")

    def test_latest_exact_decision_wins_and_feedback_is_distinct(self):
        client = FakeGitHub()
        digest = "d" * 64
        client.comments = [
            {
                "id": 1,
                "body": f"/reject-plan {digest}",
                "user": {"login": "owner"},
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 2,
                "body": "Please preserve compatibility.",
                "user": {"login": "owner"},
                "created_at": "2026-01-01T00:01:00Z",
            },
            {
                "id": 3,
                "body": f"/approve-plan {digest}",
                "user": {"login": "owner"},
                "created_at": "2026-01-01T00:02:00Z",
            },
        ]

        self.assertEqual(client.find_decision(5, digest)["decision"], "approve")
        feedback = client.find_feedback(5, after="2026-01-01T00:00:30Z")
        self.assertEqual(feedback["comment_id"], "2")
        self.assertEqual(client.decision_comments(5)[0]["body"], "Please preserve compatibility.")

    def test_malformed_control_command_is_not_feedback(self):
        client = FakeGitHub()
        digest = "d" * 64
        client.comments = [
            {
                "id": 1,
                "body": f"/approve-plan {digest[:12]}",
                "user": {"login": "owner"},
                "created_at": "2026-01-01T00:01:00Z",
            },
            {
                "id": 2,
                "body": f"/reject-plan {digest[:12]}",
                "user": {"login": "owner"},
                "created_at": "2026-01-01T00:02:00Z",
            },
            {
                "id": 3,
                "body": "/approve-plan",
                "user": {"login": "owner"},
                "created_at": "2026-01-01T00:03:00Z",
            },
            {
                "id": 4,
                "body": f"/reject-plan\t{digest[:12]}",
                "user": {"login": "owner"},
                "created_at": "2026-01-01T00:04:00Z",
            },
        ]

        self.assertIsNone(client.find_feedback(5, after="2026-01-01T00:00:00Z"))
        self.assertEqual(client.decision_comments(5), [])

    def test_plan_comment_is_deduplicated_by_immutable_marker(self):
        client = FakeGitHub()
        digest = "b" * 64
        plan = {
            "base_sha": "c" * 40,
            "summary": "summary",
            "risk": "low",
            "jobs": [
                {
                    "id": "api-5",
                    "lane": "I",
                    "title": "API",
                    "paths": ["api/a.py"],
                    "verify": "pytest",
                    "depends_on": [],
                    "contracts_read": [],
                    "contracts_changed": [],
                }
            ],
        }
        client.post_plan(5, plan, digest)
        self.assertEqual(len(client.posts), 1)
        self.assertIn(f"/reject-plan {digest}", client.posts[0][1])
        self.assertIn("non-command operator comment", client.posts[0][1])
        self.assertIn("Malformed approval or rejection commands are ignored", client.posts[0][1])
        client.comments = [{"body": f"<!-- alfred-code-plan:{digest} -->", "html_url": "existing"}]
        self.assertEqual(client.post_plan(5, plan, digest), "existing")
        self.assertEqual(len(client.posts), 1)

    def test_ci_requires_checks_and_all_success_like_conclusions(self):
        self.assertEqual(GitHubClient._ci_state([]), "PENDING")
        self.assertEqual(GitHubClient._ci_state([{"conclusion": "SUCCESS"}]), "GREEN")
        self.assertEqual(
            GitHubClient._ci_state([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]),
            "PENDING",
        )
        self.assertEqual(GitHubClient._ci_state([{"conclusion": "FAILURE"}]), "RED")


if __name__ == "__main__":
    unittest.main()
