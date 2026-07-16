import tempfile
import unittest
from pathlib import Path

from alfred_code.db import Database, SCHEMA_VERSION


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "state.sqlite3")
        self.issue = {
            "id": "I_1",
            "number": 7,
            "title": "Build thing",
            "body": "Acceptance",
            "state": "OPEN",
            "url": "https://example/issues/7",
            "labels": [{"name": "alfred-code"}],
        }
        self.database.upsert_issue(self.issue)
        self.plan = {
            "schema": 1,
            "issue": 7,
            "base_sha": "a" * 40,
            "issue_body_hash": "x",
            "summary": "plan",
            "risk": "low",
            "jobs": [
                {
                    "id": "job-7",
                    "lane": "I",
                    "title": "job",
                    "branch": "lane-1/7-job",
                    "paths": ["api/a.py"],
                    "verify": "pytest",
                    "contracts_read": [],
                    "contracts_changed": [],
                    "depends_on": [],
                    "acceptance": [],
                }
            ],
        }

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_schema_and_immutable_event_log(self):
        version = self.database.connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(self.database.events()[0]["kind"], "issue.observed")

    def test_plan_approval_jobs_and_lane_lease(self):
        self.database.save_plan(7, "p" * 64, self.plan)
        self.assertTrue(
            self.database.record_approval(7, "p" * 64, "ssdavidai", "99", None, "2026-01-01T00:00:00Z")
        )
        jobs = self.database.materialize_jobs(7, "p" * 64, self.plan)
        self.assertEqual(jobs[0]["state"], "queued")
        self.assertTrue(self.database.acquire_lane("I", "job-7"))
        self.assertEqual(self.database.lease_owner("I"), "job-7")
        self.database.release_lane("job-7")
        self.assertIsNone(self.database.lease_owner("I"))

    def test_new_plan_revokes_old_approval(self):
        self.database.save_plan(7, "a" * 64, self.plan)
        self.database.record_approval(7, "a" * 64, "ssdavidai", "1", None, "now")
        changed = dict(self.plan, summary="new")
        self.database.save_plan(7, "b" * 64, changed)
        self.assertFalse(self.database.is_approved("a" * 64))
        self.assertEqual(self.database.current_plan(7)["plan_hash"], "b" * 64)

    def test_rejection_is_durable_and_does_not_materialize_jobs(self):
        digest = "r" * 64
        self.database.save_plan(7, digest, self.plan)
        self.assertTrue(
            self.database.reject_plan(
                7,
                digest,
                "ssdavidai",
                "100",
                "https://example/reject",
                "2026-01-01T00:00:00Z",
            )
        )
        self.assertEqual(self.database.current_plan(7)["status"], "rejected")
        self.assertEqual(self.database.get_issue(7)["controller_state"], "blocked")
        with self.assertRaisesRegex(RuntimeError, "not approved"):
            self.database.materialize_jobs(7, digest, self.plan)

    def test_notification_delivery_is_deduplicated(self):
        self.assertTrue(self.database.claim_notification("key", "test", {"x": 1}))
        self.database.finish_notification("key")
        self.assertFalse(self.database.claim_notification("key", "test", {"x": 1}))


if __name__ == "__main__":
    unittest.main()
