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
        raise AssertionError(args)


class ProjectBoardTests(unittest.TestCase):
    def test_refresh_reuses_process_cache_until_forced(self):
        board = RecordingProjectBoard()

        board.refresh(3)
        board.refresh(3)

        self.assertEqual(len(board.calls), 3)
        board.refresh(3, force=True)
        self.assertEqual(len(board.calls), 6)


if __name__ == "__main__":
    unittest.main()
