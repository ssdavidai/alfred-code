import copy
import json
import re
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from alfred_code.audit import AuditLog
from alfred_code.agent_security import (
    LAUNCH_REVISION,
    LAUNCH_STATUS,
    WORKER_RESULT,
    write_launch_status,
)
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
        self.created_prs = []
        self.review_comments = []
        self.pr_calls = []

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
        self.pr_calls.append(branch)
        return self.prs.get(branch)

    def review_verdict(self, number, sha, not_before=None):
        return self.verdicts.get((number, sha))

    def pr_files(self, number):
        return ["file.txt"]

    def create_pr(self, **values):
        self.created_prs.append(copy.deepcopy(values))
        return "https://example/pr/new"

    def post_pr_comment(self, number, body):
        self.review_comments.append((number, body))
        match = re.search(r"<!-- alfred-code-review:([0-9a-f]{40,64}):(pass|fail) -->", body)
        if match:
            self.verdicts[(number, match.group(1))] = match.group(2)
        return "https://example/review-comment"


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
        self.agent_starts = 0

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

    def start_agent(self, workspace_id, agent, prompt):
        self.agent_starts += 1
        return f"retry-agent-{self.agent_starts}"

    def ensure_project(self, repo_path):
        return "project"

    def create_review_workspace(
        self,
        project_id,
        pr_number,
        name,
        branch,
        prompt,
        *,
        issue_number,
        controller_job,
        verify_command,
    ):
        self.review_creates += 1
        workspace = Workspace(f"r-{pr_number}-{self.review_creates}", name, branch, f"superset://{name}")
        self.workspaces_by_name[name] = workspace
        self.details[workspace.id] = {"id": workspace.id, "agents": [{"status": "RUNNING"}]}
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
        self.controller._prepare_exact_branch = lambda branch, head_sha: None

    def test_controller_verification_prefers_repository_ci_node_version(self):
        environment = self.controller._verification_environment()
        path = environment["PATH"].split(":")
        self.assertEqual(path[0], "/opt/homebrew/opt/node@22/bin")
        self.assertEqual(environment["npm_config_scripts_prepend_node_path"], "false")
        self.assertEqual(
            environment["npm_config_script_shell"],
            str(Path.home() / ".claude/bin/alfred-code-npm-shell"),
        )

    def test_reviewer_prompt_embeds_approved_offline_evidence(self):
        job = {
            "job_id": "api-12",
            "paths": ["file.txt"],
            "verify_command": "true",
            "contracts": {"read": ["CONTRACT.md"]},
        }
        pr = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            "b" * 40,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            "lane-1/12-api",
        )

        prompt = self.controller.reviewer_prompt(self.issue, self.plan, job, pr)

        self.assertIn("Issue title: Feature", prompt)
        self.assertIn("- works", prompt)
        self.assertIn('Contracts to verify: ["CONTRACT.md"]', prompt)
        self.assertIn("review sandbox is intentionally offline", prompt)

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

    def write_worker_lane(self):
        (self.repo / ".lane").write_text(
            json.dumps(
                {
                    "lane": "I",
                    "issue": 12,
                    "allowed": ["file.txt"],
                    "verify": "true",
                    "controller_job": "api-12",
                    "role": "worker",
                    "security_policy": "alfred-scoped-v1",
                }
            )
        )

    def write_launch_status(self, status="running", **values):
        write_launch_status(
            self.repo,
            status,
            provider="codex",
            role="worker",
            controller_job="api-12",
            started_at=values.pop("started_at", "2026-01-01T00:00:00Z"),
            **values,
        )

    def test_no_approval_means_no_job_or_workspace(self):
        result = self.controller.run_once()
        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(self.db.list_jobs(), [])
        self.assertEqual(self.superset.worker_creates, 0)

    def test_unmet_dependency_does_not_spend_a_pull_request_query(self):
        issue = self.db.upsert_issue(self.issue)
        job = {
            "job_id": "child-12",
            "state": "waiting_dependency",
            "depends_on": ["missing-parent"],
            "branch": "lane-2/12-child",
        }

        self.controller.reconcile_job(issue, self.plan, job)

        self.assertEqual(self.github.pr_calls, [])

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

    def test_prepare_exact_review_branch_is_pinned_and_immutable(self):
        branch = f"review/5-{self.sha[:12]}"
        Controller._prepare_exact_branch(self.controller, branch, self.sha)
        actual = subprocess.run(
            ["git", "rev-parse", branch],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(actual, self.sha)

    def test_clean_worker_workspace_times_out_and_releases_lane(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        self.write_launch_status(started_at="2020-01-01T00:00:00Z")
        self.db.connection.execute(
            "UPDATE jobs SET created_at = ? WHERE job_id = ?",
            ("2020-01-01T00:00:00Z", "api-12"),
        )

        self.controller.run_once()

        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "blocked")
        self.assertIn("no repository progress", job["last_error"])
        self.assertIsNone(self.db.lease_owner("I"))

    def test_exited_agent_blocks_immediately_with_real_exit_reason(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        self.write_launch_status(
            "exited",
            exit_code=70,
            reason="agent exited before writing its required result marker",
            finished_at="2026-01-01T00:00:01Z",
        )

        self.controller.run_once()

        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "blocked")
        self.assertIn("exit code 70", job["last_error"])
        self.assertIsNone(self.db.lease_owner("I"))

    def test_pre_handshake_launch_failure_is_retried_once_in_existing_workspace(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        self.db.connection.execute(
            "UPDATE jobs SET created_at = ? WHERE job_id = ?",
            ("2020-01-01T00:00:00Z", "api-12"),
        )

        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "blocked")
        self.assertIn("did not publish", self.db.get_job("api-12")["last_error"])

        self.controller.run_once()
        retried = self.db.get_job("api-12")
        self.assertEqual(retried["state"], "running")
        self.assertEqual(self.superset.agent_starts, 1)
        marker = json.loads((self.repo / LAUNCH_STATUS).read_text())
        self.assertEqual(marker["status"], "retrying")

        self.controller.run_once()
        self.assertEqual(self.superset.agent_starts, 1)

    def test_old_controller_runtime_marker_quarantine_is_retried_once(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        self.write_launch_status(
            "exited",
            exit_code=126,
            reason="agent exited before writing its required result marker",
            finished_at="2026-01-01T00:00:01Z",
        )
        self.db.update_job(
            "api-12",
            state="quarantined",
            last_error=f"workspace contains changes outside its approved plan: {LAUNCH_STATUS}",
        )
        self.db.release_lane("api-12")

        self.controller.run_once()

        retried = self.db.get_job("api-12")
        self.assertEqual(retried["state"], "running")
        self.assertEqual(self.superset.agent_starts, 1)
        marker = json.loads((self.repo / LAUNCH_STATUS).read_text())
        self.assertEqual(marker["status"], "retrying")

    def test_obsolete_launch_policy_failure_is_retried_once(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        (self.repo / "file.txt").write_text("safe in-scope progress\n")
        (self.repo / LAUNCH_STATUS).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "exited",
                    "provider": "codex",
                    "role": "worker",
                    "controller_job": "api-12",
                    "exit_code": 1,
                    "reason": "old scoped launch configuration failed",
                    "started_at": "2026-01-01T00:00:00Z",
                    "finished_at": "2026-01-01T00:00:01Z",
                }
            )
        )

        self.controller.run_once()

        retried = self.db.get_job("api-12")
        self.assertEqual(retried["state"], "running")
        self.assertEqual(self.superset.agent_starts, 1)
        marker = json.loads((self.repo / LAUNCH_STATUS).read_text())
        self.assertEqual(marker["status"], "retrying")
        self.assertEqual(marker["revision"], LAUNCH_REVISION)

    def test_obsolete_toolchain_blocker_resumes_in_scope_progress(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        (self.repo / "file.txt").write_text("safe in-scope progress\n")
        (self.repo / LAUNCH_STATUS).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "revision": 3,
                    "status": "completed",
                    "provider": "codex",
                    "role": "worker",
                    "controller_job": "api-12",
                    "exit_code": 0,
                    "reason": "agent returned after writing its result marker",
                }
            )
        )
        (self.repo / WORKER_RESULT).write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": (
                        "packages/ctrl/node_modules is a self-referential symlink, "
                        "so npm run build cannot resolve esbuild"
                    ),
                }
            )
        )

        self.controller.run_once()

        retried = self.db.get_job("api-12")
        self.assertEqual(retried["state"], "running")
        self.assertEqual(self.superset.agent_starts, 1)
        marker = json.loads((self.repo / LAUNCH_STATUS).read_text())
        self.assertEqual(marker["status"], "retrying")
        self.assertEqual(marker["revision"], LAUNCH_REVISION)
        result = json.loads((self.repo / WORKER_RESULT).read_text())
        self.assertEqual(result["status"], "retrying")

        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "running")
        self.assertEqual(self.superset.agent_starts, 1)

    def test_worker_with_uncommitted_progress_does_not_time_out(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        self.write_worker_lane()
        self.db.connection.execute(
            "UPDATE jobs SET created_at = ? WHERE job_id = ?",
            ("2020-01-01T00:00:00Z", "api-12"),
        )
        (self.repo / "file.txt").write_text("started\n")

        self.controller.run_once()

        self.assertEqual(self.db.get_job("api-12")["state"], "running")
        self.assertEqual(self.db.lease_owner("I"), "api-12")

    def test_out_of_scope_uncommitted_progress_is_quarantined(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        self.write_worker_lane()
        (self.repo / "outside.txt").write_text("escape\n")

        self.controller.run_once()

        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "quarantined")
        self.assertIn("outside its approved plan", job["last_error"])
        self.assertIsNone(self.db.lease_owner("I"))

    def test_controller_not_agent_commits_pushes_and_opens_pr_after_ready_marker(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        (self.repo / "file.txt").write_text("implemented\n")
        (self.repo / ".alfred-code-result.json").write_text(
            json.dumps({"status": "ready", "summary": "implemented"})
        )
        self.write_launch_status("completed", exit_code=0)

        self.controller.run_once()

        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "pr_open")
        self.assertEqual(job["pr_url"], "https://example/pr/new")
        self.assertEqual(self.github.created_prs[0]["branch"], "lane-1/12-api")
        remote_sha = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/lane-1/12-api"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(
            remote_sha,
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
            ).stdout.strip(),
        )

    def test_old_exit_194_finalization_retries_ready_in_scope_work_once(self):
        remote = self.root / "remote-retry.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        (self.repo / "file.txt").write_text("implemented after policy fix\n")
        (self.repo / WORKER_RESULT).write_text(json.dumps({"status": "ready", "summary": "done"}))
        self.write_launch_status("completed", exit_code=0)
        marker = json.loads((self.repo / LAUNCH_STATUS).read_text())
        marker["revision"] = LAUNCH_REVISION - 1
        (self.repo / LAUNCH_STATUS).write_text(json.dumps(marker))
        self.db.update_job(
            "api-12",
            state="blocked",
            last_error=(
                "controller finalization failed: command failed (194): "
                "bash -c cd packages/ctrl && npm run build"
            ),
        )
        self.db.release_lane("api-12")

        self.controller.run_once()

        retried = self.db.get_job("api-12")
        self.assertEqual(retried["state"], "pr_open")
        self.assertEqual(len(self.github.created_prs), 1)

    def test_failed_lane_hook_verification_is_an_obsolete_finalization_failure(self):
        job = {
            "state": "blocked",
            "last_error": (
                "controller finalization failed: command failed (1): git commit -m change\n"
                "lane verify output\nVERIFY failed: cd packages/ctrl && npm run build"
            ),
        }
        self.assertTrue(self.controller._is_obsolete_finalization_failure(job))
        job["last_error"] = "controller finalization failed: command failed (1): git push"
        self.assertFalse(self.controller._is_obsolete_finalization_failure(job))

    def test_phase0_commit_hook_skip_is_controller_only(self):
        phase0 = self.controller._trusted_commit_command(
            {"lane": "phase0"}, "contract update"
        )
        worker = self.controller._trusted_commit_command({"lane": "II"}, "worker update")
        self.assertEqual(phase0, ["git", "commit", "--no-verify", "-m", "contract update"])
        self.assertEqual(worker, ["git", "commit", "-m", "worker update"])

    def test_nested_directory_scope_quarantine_is_recoverable(self):
        job = {
            "state": "quarantined",
            "paths": ["packages/alfred-vault/tests/"],
            "last_error": (
                "workspace contains changes outside its approved plan: "
                "packages/alfred-vault/tests/test_janitor.py"
            ),
        }
        self.assertTrue(self.controller._is_directory_scope_false_quarantine(job))
        job["last_error"] = "workspace contains changes outside its approved plan: outside.txt"
        self.assertFalse(self.controller._is_directory_scope_false_quarantine(job))

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
        self.superset.workspaces_by_name.clear()
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

    def test_controller_posts_scoped_reviewer_marker_and_accepts_exact_sha(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        sha = self.sha
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
        self.controller.run_once()
        job = self.db.get_job("api-12")
        review_workspace = job["review_workspace_id"]
        self.superset.details[review_workspace]["worktreePath"] = str(self.repo)
        (self.repo / ".lane").write_text(
            json.dumps(
                {
                    "lane": "review",
                    "issue": 12,
                    "allowed": [],
                    "verify": "true",
                    "controller_job": "api-12",
                    "role": "reviewer",
                    "security_policy": "alfred-scoped-v1",
                }
            )
        )
        (self.repo / ".alfred-code-review.json").write_text(
            json.dumps({"head_sha": sha, "verdict": "pass", "findings": "No defects found."})
        )

        self.controller.run_once()

        self.assertEqual(self.db.get_job("api-12")["state"], "ready_merge")
        self.assertEqual(len(self.github.review_comments), 1)
        self.assertIn(f"<!-- alfred-code-review:{sha}:pass -->", self.github.review_comments[0][1])

    def test_reviewer_workspace_modification_is_quarantined(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        sha = self.sha
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
        self.controller.run_once()
        job = self.db.get_job("api-12")
        review_workspace = job["review_workspace_id"]
        self.superset.details[review_workspace]["worktreePath"] = str(self.repo)
        (self.repo / ".lane").write_text(
            json.dumps(
                {
                    "lane": "review",
                    "issue": 12,
                    "allowed": [],
                    "verify": "true",
                    "controller_job": "api-12",
                    "role": "reviewer",
                    "security_policy": "alfred-scoped-v1",
                }
            )
        )
        (self.repo / "file.txt").write_text("reviewer mutation\n")
        (self.repo / ".alfred-code-review.json").write_text(
            json.dumps({"head_sha": sha, "verdict": "pass", "findings": "No defects found."})
        )

        self.controller.run_once()

        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "quarantined")
        self.assertIn("read-only workspace", job["last_error"])
        self.assertEqual(self.github.review_comments, [])

    def test_exited_reviewer_blocks_immediately_with_real_exit_reason(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        sha = self.sha
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
        self.controller.run_once()
        job = self.db.get_job("api-12")
        review_workspace = job["review_workspace_id"]
        self.superset.details[review_workspace]["worktreePath"] = str(self.repo)
        (self.repo / ".lane").write_text(
            json.dumps(
                {
                    "lane": "review",
                    "issue": 12,
                    "allowed": [],
                    "verify": "true",
                    "controller_job": "api-12",
                    "role": "reviewer",
                    "security_policy": "alfred-scoped-v1",
                }
            )
        )
        write_launch_status(
            self.repo,
            "exited",
            provider="codex",
            role="reviewer",
            controller_job="api-12",
            exit_code=70,
            reason="reviewer process exited before writing its required result marker",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )

        self.controller.run_once()

        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "blocked")
        self.assertIn("reviewer process exited", job["last_error"])
        self.assertIn("exit code 70", job["last_error"])
        self.assertEqual(self.github.review_comments, [])


if __name__ == "__main__":
    unittest.main()
