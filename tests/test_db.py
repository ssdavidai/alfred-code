import sqlite3
import tempfile
import unittest
from pathlib import Path

from alfred_code.db import Database, SCHEMA, SCHEMA_VERSION


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

    def test_non_active_job_transition_releases_its_lane_atomically(self):
        digest = "p" * 64
        self.database.save_plan(7, digest, self.plan)
        self.database.record_approval(7, digest, "ssdavidai", "99", None, "now")
        self.database.materialize_jobs(7, digest, self.plan)
        self.assertTrue(self.database.acquire_lane("I", "job-7"))
        self.database.update_job("job-7", state="running")
        self.assertEqual(self.database.lease_owner("I"), "job-7")

        self.database.update_job("job-7", state="blocked", last_error="needs attention")

        self.assertIsNone(self.database.lease_owner("I"))
        released = [event for event in self.database.events() if event["kind"] == "lane.released"]
        self.assertEqual(released[-1]["detail"]["reason"], "job entered blocked")

    def test_startup_reconciliation_prunes_a_legacy_blocked_lease(self):
        digest = "p" * 64
        self.database.save_plan(7, digest, self.plan)
        self.database.record_approval(7, digest, "ssdavidai", "99", None, "now")
        self.database.materialize_jobs(7, digest, self.plan)
        self.database.update_job("job-7", state="blocked", last_error="legacy blocker")
        self.assertTrue(self.database.acquire_lane("I", "job-7"))

        removed = self.database.prune_lane_leases()

        self.assertEqual(removed[0]["job_id"], "job-7")
        self.assertEqual(removed[0]["state"], "blocked")
        self.assertIsNone(self.database.lease_owner("I"))

    def test_new_plan_revokes_old_approval(self):
        self.database.save_plan(7, "a" * 64, self.plan)
        self.database.record_approval(7, "a" * 64, "ssdavidai", "1", None, "now")
        changed = dict(self.plan, summary="new")
        self.database.save_plan(7, "b" * 64, changed)
        self.assertFalse(self.database.is_approved("a" * 64))
        self.assertEqual(self.database.current_plan(7)["plan_hash"], "b" * 64)

    def test_auto_replan_supersedes_unmerged_jobs_and_preserves_merged_history(self):
        digest = "p" * 64
        plan = dict(self.plan)
        plan["jobs"] = [
            self.plan["jobs"][0],
            {
                **self.plan["jobs"][0],
                "id": "web-7",
                "lane": "II",
                "branch": "lane-2/7-web",
                "paths": ["web/a.py"],
                "depends_on": ["job-7"],
            },
        ]
        self.database.save_plan(7, digest, plan)
        self.database.record_approval(7, digest, "ssdavidai", "99", None, "now")
        self.database.materialize_jobs(7, digest, plan)
        self.database.update_job("job-7", state="merged", pr_number=10)
        self.database.update_job("web-7", state="blocked", last_error="contract conflict")
        self.assertTrue(self.database.acquire_lane("II", "web-7"))

        changed = self.database.supersede_plan_for_replan(
            7,
            digest,
            reason="replacement required",
            blockers=[{"job_id": "web-7", "kind": "contract_plan"}],
        )

        self.assertTrue(changed)
        self.assertIsNone(self.database.current_plan(7))
        self.assertFalse(self.database.is_approved(digest))
        self.assertEqual(self.database.get_issue(7)["controller_state"], "planning")
        self.assertEqual(self.database.get_job("job-7")["state"], "merged")
        self.assertEqual(self.database.get_job("web-7")["state"], "superseded")
        self.assertEqual(self.database.get_job("web-7")["last_error"], "contract conflict")
        self.assertEqual(self.database.list_current_jobs(7), [])
        self.assertIsNone(self.database.lease_owner("II"))
        self.assertEqual(self.database.event_count(7, "plan.auto_replan_requested"), 1)

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

    def test_schema_two_migrates_durable_repair_state(self):
        path = Path(self.temp.name) / "schema-two.sqlite3"
        legacy_schema = SCHEMA
        for declaration in (
            "    base_sha TEXT,\n",
            "    repair_attempts INTEGER NOT NULL DEFAULT 0,\n",
            "    repair_sha TEXT,\n",
            "    repair_agent_id TEXT,\n",
            "    repair_requested_at TEXT,\n",
            "    repair_token TEXT,\n",
        ):
            legacy_schema = legacy_schema.replace(declaration, "")
        connection = sqlite3.connect(path)
        connection.executescript(legacy_schema)
        connection.execute("INSERT INTO schema_meta(version) VALUES (2)")
        connection.commit()
        connection.close()

        migrated = Database(path)
        self.addCleanup(migrated.close)
        columns = {
            row["name"] for row in migrated.connection.execute("PRAGMA table_info(jobs)")
        }

        self.assertEqual(
            migrated.connection.execute("SELECT version FROM schema_meta").fetchone()[0],
            SCHEMA_VERSION,
        )
        self.assertTrue(
            {
                "repair_attempts",
                "repair_sha",
                "repair_agent_id",
                "repair_requested_at",
                "repair_token",
                "base_sha",
            }.issubset(columns)
        )

    def test_schema_four_removes_lane_uniqueness_and_preserves_history_and_leases(self):
        path = Path(self.temp.name) / "schema-four.sqlite3"
        legacy = Database(path)
        legacy.upsert_issue(self.issue)
        digest = "m" * 64
        legacy.save_plan(7, digest, self.plan)
        legacy.record_approval(7, digest, "ssdavidai", "99", None, "now")
        legacy.materialize_jobs(7, digest, self.plan)
        legacy.acquire_lane("I", "job-7")
        legacy.connection.execute("UPDATE schema_meta SET version = 4")
        legacy.connection.execute(
            "CREATE UNIQUE INDEX jobs_one_lane_v4 ON jobs(issue_number, plan_hash, lane)"
        )
        legacy.close()

        migrated = Database(path)
        self.addCleanup(migrated.close)

        self.assertEqual(migrated.get_job("job-7")["state"], "queued")
        self.assertEqual(migrated.lease_owner("I"), "job-7")
        self.assertEqual(
            migrated.connection.execute("SELECT version FROM schema_meta").fetchone()[0],
            SCHEMA_VERSION,
        )
        unique_columns = []
        for index in migrated.connection.execute("PRAGMA index_list(jobs)"):
            if index["unique"]:
                unique_columns.append(
                    [
                        row["name"]
                        for row in migrated.connection.execute(
                            f"PRAGMA index_info({index['name']})"
                        )
                    ]
                )
        self.assertNotIn(["issue_number", "plan_hash", "lane"], unique_columns)

        issue = dict(self.issue, id="I_8", number=8, url="https://example/issues/8")
        migrated.upsert_issue(issue)
        sequential = dict(self.plan, issue=8)
        sequential["jobs"] = [
            dict(
                self.plan["jobs"][0],
                id="first-8",
                branch="lane-1/8-first",
            ),
            dict(
                self.plan["jobs"][0],
                id="second-8",
                branch="lane-1/8-second",
                depends_on=["first-8"],
            ),
        ]
        second_digest = "n" * 64
        migrated.save_plan(8, second_digest, sequential)
        migrated.record_approval(8, second_digest, "ssdavidai", "100", None, "now")

        jobs = migrated.materialize_jobs(8, second_digest, sequential)

        self.assertEqual([job["lane"] for job in jobs], ["I", "I"])

    def test_job_launch_base_is_immutable_once_recorded(self):
        digest = "p" * 64
        dependent = dict(self.plan)
        dependent["jobs"] = [dict(self.plan["jobs"][0], depends_on=["parent-7"])]
        self.database.save_plan(7, digest, dependent)
        self.database.record_approval(7, digest, "ssdavidai", "99", None, "now")
        job = self.database.materialize_jobs(7, digest, dependent)[0]
        self.assertIsNone(job["base_sha"])

        self.database.update_job("job-7", base_sha="b" * 40)
        self.database.update_job("job-7", base_sha="b" * 40)
        with self.assertRaisesRegex(ValueError, "base_sha is immutable"):
            self.database.update_job("job-7", base_sha="c" * 40)


if __name__ == "__main__":
    unittest.main()
