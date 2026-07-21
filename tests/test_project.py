import unittest

from alfred_code.config import GitHubConfig
from alfred_code.project import ProjectBoard


class RecordingProjectBoard(ProjectBoard):
    def __init__(self):
        super().__init__(GitHubConfig(owner="owner"))
        self.calls = []

    def _json(self, args):
        self.calls.append(tuple(args))
        command = args[1]
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


if __name__ == "__main__":
    unittest.main()
