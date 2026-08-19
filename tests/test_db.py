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

    def test_split_state_is_durable_and_requires_every_child_ready(self):
        digest = "s" * 64
        plan = {
            **self.plan,
            "story_points": 21,
            "points_evidence": "Too large for one delivery.",
            "issue_dependencies": [],
        }
        child = {
            "job_id": "job-7",
            "marker": "<!-- split:7:job-7 -->",
            "title": "job",
            "lane": "I",
        }
        self.database.save_plan(7, digest, plan)
        self.database.mark_plan_needs_split(7, digest)

        started = self.database.begin_issue_split(7, digest, [child])
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["children"][0]["spec"], child)
        with self.assertRaisesRegex(RuntimeError, "unfinished"):
            self.database.complete_issue_split(7, digest, None)

        self.database.record_split_child_created(
            7, digest, "job-7", 70, "https://example/issues/70"
        )
        self.database.record_split_child_linked(7, digest, "job-7")
        self.database.record_split_child_projected(7, digest, "job-7")
        completed = self.database.complete_issue_split(
            7, digest, "https://example/issues/7#split"
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["children"][0]["child_issue_number"], 70)
        self.assertEqual(self.database.get_issue(7)["product_stage"], "needs_split")

    def test_schema_seven_migrates_split_tables(self):
        path = Path(self.temp.name) / "schema-seven.sqlite3"
        legacy = Database(path)
        legacy.connection.execute("UPDATE schema_meta SET version=7")
        legacy.close()

        migrated = Database(path)
        self.addCleanup(migrated.close)
        tables = {
            row["name"]
            for row in migrated.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        self.assertEqual(
            migrated.connection.execute("SELECT version FROM schema_meta").fetchone()[0],
            SCHEMA_VERSION,
        )
        self.assertIn("issue_splits", tables)
        self.assertIn("issue_split_children", tables)

    def test_finalize_closed_issue_supersedes_current_plan_and_preserves_history(self):
        digest = "p" * 64
        self.database.save_plan(7, digest, self.plan)
        self.database.record_approval(7, digest, "ssdavidai", "99", None, "now")

        changed = self.database.finalize_closed_issue(
            7,
            completed=False,
            detail={"state_reason": "not_planned", "closed_by": "ssdavidai"},
        )

        self.assertTrue(changed)
        issue = self.database.get_issue(7)
        self.assertEqual(issue["controller_state"], "closed")
        self.assertEqual(issue["product_stage"], "done")
        self.assertIsNone(issue["current_plan_hash"])
        plan = self.database.connection.execute(
            "SELECT status, superseded_at FROM plans WHERE plan_hash=?", (digest,)
        ).fetchone()
        self.assertEqual(plan["status"], "superseded")
        self.assertIsNotNone(plan["superseded_at"])
        approval = self.database.connection.execute(
            "SELECT revoked_at FROM approvals WHERE plan_hash=?", (digest,)
        ).fetchone()
        self.assertIsNotNone(approval["revoked_at"])
        event = self.database.latest_event(7, "issue.closed_reconciled")
        self.assertEqual(event["detail"]["plan_disposition"], "superseded")
        self.assertEqual(event["detail"]["state_reason"], "not_planned")

    def test_finalize_completed_issue_retains_completed_plan_for_audit(self):
        digest = "p" * 64
        self.database.save_plan(7, digest, self.plan)
        self.database.record_approval(7, digest, "ssdavidai", "99", None, "now")
        self.database.materialize_jobs(7, digest, self.plan)
        self.database.update_job("job-7", state="merged")

        self.database.finalize_closed_issue(7, completed=True)

        issue = self.database.get_issue(7)
        self.assertEqual(issue["controller_state"], "completed")
        self.assertEqual(issue["current_plan_hash"], digest)
        self.assertEqual(self.database.current_plan(7)["status"], "completed")

    def test_reopen_issue_clears_completed_plan_without_erasing_history(self):
        digest = "p" * 64
        self.database.save_plan(7, digest, self.plan)
        self.database.finalize_closed_issue(7, completed=True)

        self.database.reopen_issue(7)

        issue = self.database.get_issue(7)
        self.assertEqual(issue["controller_state"], "observed")
        self.assertEqual(issue["product_stage"], "backlog")
        self.assertIsNone(issue["current_plan_hash"])
        historical = self.database.connection.execute(
            "SELECT status FROM plans WHERE plan_hash=?", (digest,)
        ).fetchone()
        self.assertEqual(historical["status"], "completed")

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

    def test_auto_replan_attempt_count_resets_after_merged_progress(self):
        self.database.event(
            "plan.auto_replan_requested",
            {"plan_hash": "a" * 64},
            issue_number=7,
        )
        self.database.event(
            "plan.auto_replan_requested",
            {"plan_hash": "b" * 64},
            issue_number=7,
        )
        self.assertEqual(self.database.auto_replan_attempt_count(7), 2)

        self.database.event(
            "job.transition",
            {"from": "ready_merge", "to": "merged"},
            issue_number=7,
        )
        self.assertEqual(self.database.auto_replan_attempt_count(7), 0)

        self.database.event(
            "plan.auto_replan_requested",
            {"plan_hash": "c" * 64},
            issue_number=7,
        )
        self.assertEqual(self.database.auto_replan_attempt_count(7), 1)

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

    def test_sprint_commitment_addition_points_and_terminal_rollover(self):
        self.database.set_product_stage(7, "sprint_queue", rank=2)
        sprint = self.database.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[7],
        )
        self.assertEqual(sprint["number"], 0)
        self.assertEqual(self.database.get_issue(7)["product_stage"], "active")
        self.assertEqual(self.database.get_issue(7)["controller_state"], "planning")
        self.assertEqual(self.database.sprint_items(sprint["id"])[0]["commitment"], "committed")

        second = dict(self.issue, id="I_8", number=8, url="https://example/issues/8")
        self.database.upsert_issue(second)
        added = self.database.add_to_active_sprint(8)
        self.assertEqual(added["commitment"], "added")
        self.assertEqual(added["rank"], 1)

        self.database.record_story_points(7, 5, "one lane with contract reads")
        self.database.record_story_points(8, 3, "bounded single-lane change")
        self.database.set_sprint_item_status(7, "done")
        self.database.set_sprint_item_status(8, "blocked")
        closed, items = self.database.close_active_sprint()

        self.assertEqual(closed["state"], "closed")
        self.assertIsNone(self.database.active_sprint())
        self.assertEqual([item["story_points"] for item in items], [5, 3])
        self.assertEqual(self.database.get_issue(7)["product_stage"], "done")
        self.assertEqual(self.database.get_issue(8)["product_stage"], "inbox")
        self.assertEqual(self.database.get_issue(8)["carryover_replan"], 1)
        self.assertEqual(self.database.get_issue(8)["controller_state"], "planning")

    def test_sprint_integration_and_retro_state_are_durable(self):
        self.database.set_product_stage(7, "sprint_queue")
        sprint = self.database.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[7],
        )

        updated = self.database.update_sprint(
            sprint["id"],
            base_sha="a" * 40,
            branch="alfred-code/sprint-0-integration",
            workspace_id="workspace-0",
            integration_head_sha="b" * 40,
            delivery_pr_number=99,
            delivery_pr_url="https://example/pr/99",
            delivery_sha="b" * 40,
            retro_state="passed",
            retro_sha="b" * 40,
            retro_verdict="pass",
            retro_findings="combined verification passed",
        )

        self.assertEqual(updated["branch"], "alfred-code/sprint-0-integration")
        self.assertEqual(updated["delivery_pr_number"], 99)
        self.assertEqual(updated["retro_verdict"], "pass")
        self.assertEqual(
            self.database.current_sprint_item(7)["sprint_head_sha"], "b" * 40
        )

    def test_manually_closed_sprint_item_is_terminal_without_carryover(self):
        self.database.set_product_stage(7, "sprint_queue")
        sprint = self.database.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[7],
        )
        self.database.set_sprint_item_status(7, "closed")

        self.database.close_active_sprint()

        issue = self.database.get_issue(7)
        self.assertEqual(issue["product_stage"], "done")
        self.assertEqual(issue["carryover_replan"], 0)
        self.assertEqual(self.database.sprint_items(sprint["id"])[0]["status"], "closed")

    def test_blocked_sprint_carryover_retires_failed_graph_for_inbox_replan(self):
        self.database.set_product_stage(7, "sprint_queue")
        sprint = self.database.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[7],
        )
        digest = "c" * 64
        self.database.save_plan(7, digest, self.plan)
        self.database.record_approval(7, digest, "ssdavidai", "99", None, "now")
        self.database.materialize_jobs(7, digest, self.plan)
        self.database.update_job("job-7", state="blocked", last_error="review failed")
        self.assertTrue(self.database.acquire_lane("I", "job-7"))
        self.database.set_sprint_item_status(7, "blocked")

        self.database.close_active_sprint()

        issue = self.database.get_issue(7)
        self.assertEqual(issue["product_stage"], "inbox")
        self.assertEqual(issue["controller_state"], "planning")
        self.assertEqual(issue["carryover_replan"], 1)
        self.assertIsNone(self.database.current_plan(7))
        self.assertFalse(self.database.is_approved(digest))
        self.assertEqual(self.database.get_job("job-7")["state"], "superseded")
        self.assertEqual(self.database.get_job("job-7")["last_error"], "review failed")
        self.assertIsNone(self.database.lease_owner("I"))
        self.assertEqual(
            self.database.event_count(7, "sprint.carryover_replan_requested"),
            1,
        )
        self.assertEqual(self.database.sprint_items(sprint["id"])[0]["status"], "blocked")

    def test_schema_six_adds_disabled_carryover_replan_flag(self):
        path = Path(self.temp.name) / "schema-six.sqlite3"
        legacy = Database(path)
        legacy.upsert_issue(self.issue)
        legacy.connection.execute("ALTER TABLE issues DROP COLUMN carryover_replan")
        legacy.connection.execute("UPDATE schema_meta SET version=6")
        legacy.close()

        migrated = Database(path)
        self.addCleanup(migrated.close)

        self.assertEqual(migrated.get_issue(7)["carryover_replan"], 0)
        self.assertEqual(
            migrated.connection.execute("SELECT version FROM schema_meta").fetchone()[0],
            SCHEMA_VERSION,
        )

    def test_schema_five_migrates_delivery_state_without_enrolling_old_backlog(self):
        path = Path(self.temp.name) / "schema-five.sqlite3"
        legacy = Database(path)
        legacy.upsert_issue(self.issue)
        legacy.set_issue_state(7, "planning")
        legacy.connection.execute("DROP TABLE sprint_items")
        legacy.connection.execute("DROP TABLE sprints")
        legacy.connection.execute("ALTER TABLE issues DROP COLUMN project_rank")
        legacy.connection.execute("ALTER TABLE issues DROP COLUMN product_stage")
        legacy.connection.execute("UPDATE schema_meta SET version=5")
        legacy.close()

        migrated = Database(path)
        self.addCleanup(migrated.close)

        self.assertEqual(migrated.get_issue(7)["product_stage"], "inbox")
        self.assertEqual(migrated.list_sprints(), [])
        self.assertEqual(
            migrated.connection.execute("SELECT version FROM schema_meta").fetchone()[0],
            SCHEMA_VERSION,
        )

    def test_sprint_start_retires_unestimated_pre_scheduler_plan(self):
        digest = "u" * 64
        self.database.save_plan(7, digest, self.plan)
        self.database.set_product_stage(7, "sprint_queue")

        self.database.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[7],
        )

        self.assertIsNone(self.database.current_plan(7))
        status = self.database.connection.execute(
            "SELECT status FROM plans WHERE plan_hash=?", (digest,)
        ).fetchone()["status"]
        self.assertEqual(status, "superseded")
        self.assertEqual(self.database.get_issue(7)["controller_state"], "planning")


if __name__ == "__main__":
    unittest.main()
