import copy
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from alfred_code.audit import AuditLog
from alfred_code.config import ControllerConfig, GitHubConfig, SlackConfig, SupersetConfig
from alfred_code.controller import Controller
from alfred_code.db import Database
from alfred_code.github import PullRequestObservation
from alfred_code.notify import DurableNotifier
from alfred_code.superset import Workspace
from alfred_code.util import content_hash


class RecordingChannel:
    channel = "test"

    def __init__(self):
        self.messages = []

    def send(self, message, detail):
        self.messages.append((message, detail))


class FakeGitHub:
    def __init__(self, issue, sha):
        self.issue_value = issue
        self.extra_issues = {}
        self.sha = sha
        self.approval = None
        self.rejection = None
        self.feedback = None
        self.prs = {}
        self.verdicts = {}
        self.plans = []

    def intake_issues(self):
        return [copy.deepcopy(self.issue_value)] if self.issue_value["state"] == "OPEN" else []

    def open_issues(self):
        issues = [self.issue_value, *self.extra_issues.values()]
        return [copy.deepcopy(issue) for issue in issues if issue["state"] == "OPEN"]

    def issue(self, number):
        if number == int(self.issue_value["number"]):
            return copy.deepcopy(self.issue_value)
        return copy.deepcopy(self.extra_issues[number])

    def default_branch_sha(self):
        return self.sha

    def post_plan(self, issue_number, plan, plan_hash):
        self.plans.append(plan_hash)
        return "https://example/plan"

    def find_approval(self, issue_number, plan_hash):
        return copy.deepcopy(self.approval)

    def find_decision(self, issue_number, plan_hash):
        if self.rejection is not None:
            return {"decision": "reject", **copy.deepcopy(self.rejection)}
        if self.approval is not None:
            return {"decision": "approve", **copy.deepcopy(self.approval)}
        return None

    def find_feedback(self, issue_number, after):
        return copy.deepcopy(self.feedback)

    def pr_for_branch(self, branch):
        return self.prs.get(branch)

    def review_verdict(self, number, sha, not_before=None):
        return self.verdicts.get((number, sha))

    def pr_files(self, number):
        return ["file.txt"]


class FakePlanner:
    def __init__(self, plan, plan_hash):
        self.plan = plan
        self.plan_hash = plan_hash
        self.calls = 0

    def plan_issue(self, issue_number):
        self.calls += 1
        return copy.deepcopy(self.plan), self.plan_hash

    def revalidate(self, plan, expected_hash):
        if content_hash(plan) != expected_hash:
            raise AssertionError("corrupt test plan")


class FakeSuperset:
    def __init__(self):
        self.workspaces_by_name = {}
        self.details = {}
        self.deleted = []
        self.worker_creates = 0
        self.review_creates = 0

    def workspace_by_name(self, name):
        return self.workspaces_by_name.get(name)

    def create_worker(self, repo_path, issue_number, job, prompt):
        self.worker_creates += 1
        name = f"alfred-code-{issue_number}-{job['lane'].lower()}"
        workspace = Workspace(f"w-{job['job_id']}", name, job["branch"], f"superset://{name}")
        self.workspaces_by_name[name] = workspace
        self.details[workspace.id] = {"id": workspace.id, "agents": [{"status": "RUNNING"}]}
        return workspace, f"a-{job['job_id']}"

    def workspace_details(self, workspace_id):
        return self.details[workspace_id]

    def ensure_project(self, repo_path):
        return "project"

    def create_review_workspace(self, project_id, pr_number, name, prompt):
        self.review_creates += 1
        workspace = Workspace(f"r-{pr_number}-{self.review_creates}", name, "review", f"superset://{name}")
        self.workspaces_by_name[name] = workspace
        return workspace, f"review-agent-{self.review_creates}"

    def delete_workspace(self, workspace_id):
        self.deleted.append(workspace_id)


class FakeProject:
    def __init__(self):
        self.refreshes = []
        self.synced = {}
        self.history = []

    def refresh(self, number):
        self.refreshes.append(number)

    def sync_issue(self, **values):
        self.history.append(copy.deepcopy(values))
        self.synced[values["issue_url"]] = copy.deepcopy(values)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "file.txt").write_text("base\n")
        subprocess.run(["git", "add", "file.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repo, check=True, capture_output=True)
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        self.issue = {
            "id": "I_12",
            "number": 12,
            "title": "Feature",
            "body": "Build it",
            "state": "OPEN",
            "url": "https://example/issues/12",
            "labels": [{"name": "alfred-code"}],
        }
        self.plan = {
            "schema": 1,
            "issue": 12,
            "base_sha": self.sha,
            "issue_body_hash": content_hash("Build it"),
            "summary": "API",
            "risk": "low",
            "jobs": [
                {
                    "id": "api-12",
                    "lane": "I",
                    "title": "API",
                    "branch": "lane-1/12-api",
                    "paths": ["file.txt"],
                    "verify": "true",
                    "contracts_read": [],
                    "contracts_changed": [],
                    "depends_on": [],
                    "acceptance": ["works"],
                }
            ],
        }
        self.plan_hash = content_hash(self.plan)
        self.config = ControllerConfig(
            repo_path=self.repo,
            state_dir=self.root / "state",
            apply=True,
            github=GitHubConfig(repo="owner/repo", owner="owner", approvers=("owner",)),
            superset=SupersetConfig(cli="superset"),
            slack=SlackConfig(),
        )
        self.db = Database(self.config.database_path)
        self.github = FakeGitHub(self.issue, self.sha)
        self.superset = FakeSuperset()
        self.planner = FakePlanner(self.plan, self.plan_hash)
        self.channel = RecordingChannel()
        self.controller = Controller(
            self.config,
            self.db,
            self.github,
            self.superset,
            self.planner,
            DurableNotifier(self.db, self.channel),
            audit=AuditLog(self.root / "audit.jsonl"),
        )

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def approve(self):
        self.github.approval = {
            "actor": "owner",
            "comment_id": "1",
            "url": "https://example/approval",
            "created_at": "2026-01-01T00:00:00Z",
        }

    def test_no_approval_means_no_job_or_workspace(self):
        result = self.controller.run_once()
        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(self.db.list_jobs(), [])
        self.assertEqual(self.superset.worker_creates, 0)

    def test_exact_rejection_blocks_without_materializing_jobs(self):
        self.controller.run_once()
        self.github.rejection = {
            "actor": "owner",
            "comment_id": "reject-1",
            "url": "https://example/rejection",
            "created_at": "2026-01-01T00:00:00Z",
        }

        result = self.controller.run_once()

        self.assertEqual(result["issues"][0]["state"], "blocked")
        self.assertEqual(self.db.current_plan(12)["status"], "rejected")
        self.assertEqual(self.db.list_jobs(), [])
        self.assertEqual(self.superset.worker_creates, 0)

    def test_operator_feedback_replans_before_approval(self):
        first = self.controller.run_once()["issues"][0]["plan_hash"]
        self.github.feedback = {
            "actor": "owner",
            "comment_id": "feedback-1",
            "url": "https://example/feedback",
            "created_at": "2026-01-02T00:00:00Z",
            "body": "Keep the API backwards compatible.",
        }
        self.plan = copy.deepcopy(self.plan)
        self.plan["summary"] = "Revised API"
        self.planner.plan = copy.deepcopy(self.plan)
        self.planner.plan_hash = content_hash(self.plan)

        result = self.controller.run_once()

        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertNotEqual(result["issues"][0]["plan_hash"], first)
        self.assertEqual(self.planner.calls, 2)

    def test_dry_run_only_observes(self):
        self.controller.config = replace(self.config, apply=False)
        self.controller.run_once()
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.db.get_issue(12)["controller_state"], "observed")

    def test_open_backlog_is_projected_without_enrolling_unlabeled_issues(self):
        backlog = {
            "id": "I_13",
            "number": 13,
            "title": "Backlog item",
            "body": "Keep visible without execution",
            "state": "OPEN",
            "url": "https://example/issues/13",
            "labels": [{"name": "bug"}],
        }
        self.github.extra_issues[13] = backlog
        project = FakeProject()
        self.controller.project = project
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
        )

        result = self.controller.run_once()

        self.assertEqual([item["number"] for item in result["issues"]], [12])
        self.assertEqual(self.planner.calls, 1)
        self.assertEqual(self.db.get_issue(13)["controller_state"], "observed")
        self.assertEqual(project.refreshes, [3])
        self.assertEqual(
            project.synced["https://example/issues/13"]["controller_state"],
            "observed",
        )
        self.assertIsNone(self.db.current_plan(13))

        self.github.extra_issues[13]["state"] = "CLOSED"
        self.controller.run_once()
        self.assertEqual(self.db.get_issue(13)["controller_state"], "closed")
        self.assertEqual(
            project.synced["https://example/issues/13"]["controller_state"],
            "closed",
        )

    def test_auto_intake_plans_unlabeled_issue_and_projects_specifying_first(self):
        self.issue["labels"] = [{"name": "bug"}]
        project = FakeProject()
        self.controller.project = project
        self.controller.config = replace(
            self.config,
            github=replace(
                self.config.github,
                auto_intake=True,
                project_number=3,
            ),
        )

        result = self.controller.run_once()

        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(self.planner.calls, 1)
        projected_states = [
            item["controller_state"]
            for item in project.history
            if item["issue_url"] == self.issue["url"]
        ]
        self.assertEqual(projected_states, ["planning", "awaiting_approval"])

    def test_approval_launches_once_and_restart_adopts_workspace(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "running")
        self.assertEqual(self.superset.worker_creates, 1)
        self.controller.run_once()
        self.assertEqual(self.superset.worker_creates, 1)

    def test_clean_worker_workspace_times_out_and_releases_lane(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        self.db.connection.execute(
            "UPDATE jobs SET created_at = ? WHERE job_id = ?",
            ("2020-01-01T00:00:00Z", "api-12"),
        )

        self.controller.run_once()

        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "blocked")
        self.assertIn("no repository progress", job["last_error"])
        self.assertIsNone(self.db.lease_owner("I"))

    def test_worker_with_uncommitted_progress_does_not_time_out(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        self.db.connection.execute(
            "UPDATE jobs SET created_at = ? WHERE job_id = ?",
            ("2020-01-01T00:00:00Z", "api-12"),
        )
        (self.repo / "progress.txt").write_text("started\n")

        self.controller.run_once()

        self.assertEqual(self.db.get_job("api-12")["state"], "running")
        self.assertEqual(self.db.lease_owner("I"), "api-12")

    def test_pr_ci_review_sha_and_merge_lifecycle(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        pending = PullRequestObservation(5, "https://example/pr/5", "OPEN", "b" * 40, "PENDING", "BLOCKED", "MERGEABLE", False, branch)
        self.github.prs[branch] = pending
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "pr_open")
        green = PullRequestObservation(
            5,
            pending.url,
            "OPEN",
            pending.head_sha,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "## Smoke evidence\nreal output",
        )
        self.github.prs[branch] = green
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "reviewing")
        self.assertEqual(self.superset.review_creates, 1)
        self.controller.run_once()
        self.assertEqual(self.superset.review_creates, 1)
        self.github.verdicts[(5, green.head_sha)] = "pass"
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "ready_merge")
        changed = PullRequestObservation(
            5,
            pending.url,
            "OPEN",
            "c" * 40,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "## Smoke evidence\nreal output",
        )
        self.github.prs[branch] = changed
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["review_sha"], "c" * 40)
        self.assertEqual(self.superset.review_creates, 2)
        self.github.verdicts[(5, changed.head_sha)] = "pass"
        self.github.prs[branch] = PullRequestObservation(5, pending.url, "MERGED", changed.head_sha, "GREEN", "CLEAN", "MERGEABLE", False, branch)
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "merged")
        self.assertIsNone(self.db.lease_owner("I"))
        self.assertEqual(self.superset.deleted, [])

    def test_issue_scope_drift_blocks_active_work(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        self.github.issue_value["body"] = "Different scope"
        self.controller.run_once()
        self.assertEqual(self.db.get_issue(12)["controller_state"], "blocked")

    def test_closed_issue_with_open_pr_is_quarantined(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        self.github.prs[branch] = PullRequestObservation(5, "https://example/pr/5", "OPEN", "b" * 40, "GREEN", "CLEAN", "MERGEABLE", False, branch)
        self.github.issue_value["state"] = "CLOSED"
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "quarantined")
        self.assertEqual(self.db.get_issue(12)["controller_state"], "closed")

    def test_passing_review_cannot_ready_a_draft(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        sha = "d" * 40
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            sha,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            True,
            branch,
            "## Smoke evidence\nreal output",
        )
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "reviewing")
        self.github.verdicts[(5, sha)] = "pass"
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "pr_open")
        self.assertEqual(job["last_error"], "PR is still a draft")

    def test_out_of_plan_pr_file_blocks_before_review(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        sha = "e" * 40
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            sha,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "## Smoke evidence\nreal output",
        )
        self.github.pr_files = lambda number: ["forbidden/escape.py"]
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "blocked")
        self.assertIn("outside its approved plan", job["last_error"])

    def test_green_pr_without_smoke_section_blocks(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        self.github.prs[branch] = PullRequestObservation(
            5, "https://example/pr/5", "OPEN", "f" * 40, "GREEN", "CLEAN", "MERGEABLE", False, branch
        )
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["last_error"], "PR body has no ## Smoke evidence section")


if __name__ == "__main__":
    unittest.main()
