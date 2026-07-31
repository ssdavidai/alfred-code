import unittest

from alfred_code.config import GitHubConfig
from alfred_code.errors import AuthorityUnavailable
from alfred_code.project import ProjectBoard


class RecordingProjectBoard(ProjectBoard):
    def __init__(self):
        super().__init__(GitHubConfig(owner="owner"))
        self.calls = []
        self.fail_command = None

    def _json(self, args):
        self.calls.append(tuple(args))
        command = args[1]
        if command == self.fail_command:
            raise AuthorityUnavailable(f"{command} failed")
        if command == "view":
            return {"id": "project-id", "number": 3}
        if command == "field-list":
            return {"fields": []}
        if command == "item-list":
            return {"items": []}
        if command == "item-edit":
            return {}
        raise AssertionError(args)


class ProjectBoardTests(unittest.TestCase):
    def test_refresh_reuses_process_cache_until_forced(self):
        board = RecordingProjectBoard()

        board.refresh(3)
        board.refresh(3)

        self.assertEqual(len(board.calls), 3)
        board.refresh(3, force=True)
        self.assertEqual(len(board.calls), 6)

    def test_failed_forced_refresh_preserves_last_good_snapshot(self):
        board = RecordingProjectBoard()
        board.refresh(3)
        previous = (
            board._projects[3],
            board._fields[3],
            board._items[3],
            board._ordered_items[3],
        )
        board.fail_command = "item-list"

        with self.assertRaises(AuthorityUnavailable):
            board.refresh(3, force=True)

        self.assertIs(board._projects[3], previous[0])
        self.assertIs(board._fields[3], previous[1])
        self.assertIs(board._items[3], previous[2])
        self.assertIs(board._ordered_items[3], previous[3])

    def test_sync_explicitly_clears_stale_optional_text_fields(self):
        board = RecordingProjectBoard()
        issue_url = "https://example/issues/7"
        board._projects[3] = {"id": "project-id", "number": 3}
        board._fields[3] = {
            "Plan hash": {"id": "plan-field", "name": "Plan hash", "dataType": "TEXT"},
            "Runtime": {"id": "runtime-field", "name": "Runtime", "dataType": "TEXT"},
        }
        board._items[3] = {
            issue_url: {
                "id": "item-id",
                "plan hash": "old-plan",
                "runtime": "I:blocked",
            }
        }

        board.sync_issue(
            project_number=3,
            issue_url=issue_url,
            controller_state="planning",
            plan_hash="",
            runtime="",
        )

        edits = [call for call in board.calls if call[1] == "item-edit"]
        self.assertEqual(len(edits), 2)
        self.assertTrue(all("--clear" in call for call in edits))
        self.assertEqual(board._items[3][issue_url]["plan hash"], "")
        self.assertEqual(board._items[3][issue_url]["runtime"], "")

    def test_delivery_items_preserve_github_manual_position(self):
        board = RecordingProjectBoard()
        board._ordered_items[3] = [
            {
                "id": "second-item",
                "control stage": "Inbox",
                "content": {"number": 9, "url": "https://example/issues/9"},
            },
            {
                "id": "first-item",
                "control stage": "Sprint queue",
                "story points": 5,
                "content": {"number": 7, "url": "https://example/issues/7"},
            },
        ]

        items = board.delivery_items(3)

        self.assertEqual([item["issue_number"] for item in items], [9, 7])
        self.assertEqual([item["rank"] for item in items], [0, 1])
        self.assertEqual(items[1]["stage"], "Sprint queue")

    def test_story_points_and_iteration_use_typed_project_edits(self):
        board = RecordingProjectBoard()
        issue_url = "https://example/issues/7"
        board._projects[3] = {"id": "project-id", "number": 3}
        board._fields[3] = {
            "Story points": {"id": "points", "name": "Story points", "type": "ProjectV2Field"},
            "Sprint": {"id": "sprint", "name": "Sprint", "type": "ProjectV2IterationField"},
        }
        board._items[3] = {issue_url: {"id": "item-id"}}

        board.sync_issue(
            project_number=3,
            issue_url=issue_url,
            controller_state="planning",
            story_points=8,
            iteration_id="iteration-0",
        )

        edits = [call for call in board.calls if call[1] == "item-edit"]
        self.assertTrue(any(("--number", "8") == call[call.index("--number"):call.index("--number") + 2] for call in edits))
        self.assertTrue(any("--iteration-id" in call and "iteration-0" in call for call in edits))


if __name__ == "__main__":
    unittest.main()
