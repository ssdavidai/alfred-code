import json
import tempfile
import unittest
from pathlib import Path

from alfred_code.db import Database
from alfred_code.legacy import LegacyImporter
from alfred_code.notify import DurableNotifier


class RecordingNotifier:
    channel = "test"

    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send(self, message, detail):
        self.messages.append((message, detail))
        if self.fail:
            raise RuntimeError("offline")


class NotifyLegacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "state.sqlite3")

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_notifier_failure_does_not_raise_and_can_retry(self):
        channel = RecordingNotifier(fail=True)
        notifier = DurableNotifier(self.database, channel)
        self.assertFalse(notifier.send("x", "hello"))
        channel.fail = False
        self.assertTrue(notifier.send("x", "hello"))
        self.assertFalse(notifier.send("x", "hello"))

    def test_legacy_import_is_evidence_only_and_idempotent(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "dispatched.json").write_text(json.dumps([{"issue": 9, "status": "building"}]))
        importer = LegacyImporter(self.database, legacy)
        first = importer.run()
        second = importer.run()
        self.assertEqual(first[0]["status"], "observed")
        self.assertEqual(second[0]["status"], "unchanged")
        self.assertEqual(self.database.list_issues(), [])


if __name__ == "__main__":
    unittest.main()

