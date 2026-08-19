import copy
import json
import re
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
from alfred_code.errors import AuthorityUnavailable
from alfred_code.github import PullRequestObservation
from alfred_code.notify import DurableNotifier
from alfred_code.superset import Workspace, worker_workspace_name
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
        self.feedbacks = {}
        self.plans = []
        self.created_prs = []
        self.review_comments = []
        self.pr_calls = []
        self.closed_issues = []
        self.reopened_issues = []
        self.updated_pr_bodies = []
        self.invalidated_prs = []
        self.auto_replans = []
        self.closed_prs = []
        self.failure_evidence = "gitleaks failed at docs/contract.md:234"

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

    def refresh_default_branch_sha(self):
        return self.sha

    def close_issue(self, number):
        self.closed_issues.append(number)
        self.issue_value["state"] = "CLOSED"

    def reopen_issue(self, number):
        self.reopened_issues.append(number)
        self.issue_value["state"] = "OPEN"

    def update_pr_body(self, number, body):
        self.updated_pr_bodies.append((number, body))
        for branch, pr in list(self.prs.items()):
            if pr and pr.number == number:
                self.prs[branch] = replace(pr, body=body)

    def post_plan(self, issue_number, plan, plan_hash):
        self.plans.append(plan_hash)
        return "https://example/plan"

    def post_auto_replan(self, issue_number, plan_hash, blockers, completed):
        self.auto_replans.append(
            (issue_number, plan_hash, copy.deepcopy(blockers), copy.deepcopy(completed))
        )
        return "https://example/replan"

    def find_approval(self, issue_number, plan_hash):
        return copy.deepcopy(self.approval)

    def find_decision(self, issue_number, plan_hash):
        if self.rejection is not None:
            rejection = copy.deepcopy(self.rejection)
            if rejection.pop("plan_hash", plan_hash) != plan_hash:
                return None
            return {"decision": "reject", **rejection}
        if self.approval is not None:
            approval = copy.deepcopy(self.approval)
            if approval.pop("plan_hash", plan_hash) != plan_hash:
                return None
            return {"decision": "approve", **approval}
        return None

    def find_feedback(self, issue_number, after):
        return copy.deepcopy(self.feedback)

    def pr_for_branch(self, branch):
        self.pr_calls.append(branch)
        return self.prs.get(branch)

    def invalidate_pr(self, branch):
        self.invalidated_prs.append(branch)

    def review_verdict(self, number, sha, not_before=None):
        return self.verdicts.get((number, sha))

    def review_feedback(self, number, sha, not_before=None):
        if (number, sha) in self.feedbacks:
            return copy.deepcopy(self.feedbacks[(number, sha)])
        verdict = self.verdicts.get((number, sha))
        return {"verdict": verdict, "body": f"review verdict: {verdict}"} if verdict else None

    def pr_files(self, number):
        return ["file.txt"]

    def pr_failure_evidence(self, pr):
        return self.failure_evidence

    def create_pr(self, **values):
        self.created_prs.append(copy.deepcopy(values))
        return "https://example/pr/new"

    def post_pr_comment(self, number, body):
        self.review_comments.append((number, body))
        match = re.search(r"<!-- alfred-code-review:([0-9a-f]{40,64}):(pass|fail) -->", body)
        if match:
            self.verdicts[(number, match.group(1))] = match.group(2)
        return "https://example/review-comment"

    def close_pr(self, number, comment):
        self.closed_prs.append((number, comment))
        for branch, pr in list(self.prs.items()):
            if pr and pr.number == number:
                self.prs[branch] = replace(pr, state="CLOSED")


class FakePlanner:
    def __init__(self, plan, plan_hash):
        self.plan = plan
        self.plan_hash = plan_hash
        self.calls = 0

    def plan_issue(self, issue_number):
        self.calls += 1
        return copy.deepcopy(self.plan), self.plan_hash

    def prepare_plans(self, issue_numbers):
        return [SimpleNamespace(issue_number=number) for number in issue_numbers]

    def execute_plan(self, prepared):
        return self.plan_issue(prepared.issue_number)

    def revalidate(self, plan, expected_hash):
        if content_hash(plan) != expected_hash:
            raise AssertionError("corrupt test plan")


class ConcurrentPlanner:
    def __init__(self, sha, bodies, expected_parallel):
        self.sha = sha
        self.bodies = bodies
        self.expected_parallel = expected_parallel
        self.lock = threading.Lock()
        self.all_started = threading.Event()
        self.active = 0
        self.maximum_active = 0
        self.calls = []

    def prepare_plans(self, issue_numbers):
        return [SimpleNamespace(issue_number=number) for number in issue_numbers]

    def execute_plan(self, prepared):
        number = prepared.issue_number
        with self.lock:
            self.calls.append(number)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == self.expected_parallel:
                self.all_started.set()
        self.all_started.wait(timeout=2)
        time.sleep(0.02)
        plan = {
            "schema": 1,
            "issue": number,
            "base_sha": self.sha,
            "issue_body_hash": content_hash(self.bodies[number]),
            "summary": f"Plan {number}",
            "risk": "low",
            "jobs": [
                {
                    "id": f"api-{number}",
                    "lane": "I",
                    "title": f"API {number}",
                    "branch": f"lane-1/{number}-api",
                    "paths": ["file.txt"],
                    "verify": "true",
                    "contracts_read": [],
                    "contracts_changed": [],
                    "depends_on": [],
                    "acceptance": ["works"],
                }
            ],
        }
        with self.lock:
            self.active -= 1
        return plan, content_hash(plan)

    def plan_issue(self, issue_number):
        return self.execute_plan(SimpleNamespace(issue_number=issue_number))

    def revalidate(self, plan, expected_hash):
        if content_hash(plan) != expected_hash:
            raise AssertionError("corrupt test plan")


class BlockingPlanner(ConcurrentPlanner):
    def __init__(self, sha, bodies):
        super().__init__(sha, bodies, expected_parallel=1)
        self.started = threading.Event()
        self.release = threading.Event()

    def execute_plan(self, prepared):
        self.started.set()
        self.release.wait(timeout=2)
        return super().execute_plan(prepared)


class FakeSuperset:
    def __init__(self):
        self.workspaces_by_name = {}
        self.details = {}
        self.deleted = []
        self.worker_creates = 0
        self.review_creates = 0
        self.agent_starts = 0
        self.agent_prompts = []

    def workspace_by_name(self, name):
        return self.workspaces_by_name.get(name)

    def create_worker(self, repo_path, issue_number, job, prompt):
        self.worker_creates += 1
        name = worker_workspace_name(
            "alfred-code", issue_number, job["lane"], job["job_id"]
        )
        workspace = Workspace(f"w-{job['job_id']}", name, job["branch"], f"superset://{name}")
        self.workspaces_by_name[name] = workspace
        self.details[workspace.id] = {"id": workspace.id, "agents": [{"status": "RUNNING"}]}
        return workspace, f"a-{job['job_id']}"

    def workspace_details(self, workspace_id):
        return self.details[workspace_id]

    def start_agent(self, workspace_id, agent, prompt):
        self.agent_starts += 1
        self.agent_prompts.append(prompt)
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
        self.refresh_error = None
        self.synced = {}
        self.history = []
        self.items = []
        self.moved_to_top = []

    def refresh(self, number, *, force=False):
        self.refreshes.append(number)
        if self.refresh_error:
            raise self.refresh_error

    def delivery_items(self, number):
        return copy.deepcopy(self.items)

    def move_to_top(self, number, urls):
        self.moved_to_top.append((number, list(urls)))

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
        self.controller._sync_source_checkout = lambda: self.sha
        self.controller._prepare_exact_branch = lambda branch, head_sha: None

    def test_controller_verification_prefers_repository_ci_node_version(self):
        environment = self.controller._verification_environment()
        path = environment["PATH"].split(":")
        self.assertEqual(path[0], "/opt/homebrew/opt/node@22/bin")
        self.assertEqual(environment["npm_config_scripts_prepend_node_path"], "false")
        self.assertEqual(
            environment["npm_config_script_shell"],
            str((Path.home() / ".claude/bin/alfred-code-npm-shell").resolve()),
        )

    def test_cycle_fails_before_issue_observation_when_source_sync_is_unsafe(self):
        def refuse_sync():
            raise AuthorityUnavailable("controller source checkout is dirty")

        self.controller._sync_source_checkout = refuse_sync

        with self.assertRaisesRegex(AuthorityUnavailable, "dirty"):
            self.controller.run_once()

        self.assertEqual(self.db.list_issues(), [])
        failures = [
            event
            for event in self.db.events(20)
            if event["kind"] == "reconcile.authority_failed"
        ]
        self.assertEqual(failures, [])
        audit = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertTrue(
            any(
                event["kind"] == "reconcile.authority_failed"
                and event["authority"] == "repository"
                for event in audit
            )
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
            "plan_hash": self.github.plans[-1] if self.github.plans else self.plan_hash,
        }

    def add_dependent_job(self):
        self.plan["jobs"].append(
            {
                "id": "web-12",
                "lane": "II",
                "title": "Web",
                "branch": "lane-2/12-web",
                "paths": ["file.txt"],
                "verify": "true",
                "contracts_read": [],
                "contracts_changed": [],
                "depends_on": ["api-12"],
                "acceptance": ["uses the API"],
            }
        )
        self.plan_hash = content_hash(self.plan)
        self.planner.plan_hash = self.plan_hash

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

    def record_split_dependency(self, dependency_number=11):
        parent_issue = {
            "id": "I_42",
            "number": 42,
            "title": "Epic",
            "body": "Split this",
            "state": "OPEN",
            "url": "https://example/issues/42",
            "labels": [],
        }
        dependency_issue = {
            "id": f"I_{dependency_number}",
            "number": dependency_number,
            "title": "Contract child",
            "body": "Contract",
            "state": "OPEN",
            "url": f"https://example/issues/{dependency_number}",
            "labels": [],
        }
        self.db.upsert_issue(parent_issue)
        self.db.upsert_issue(dependency_issue)
        self.github.extra_issues[42] = {**copy.deepcopy(parent_issue), "state": "CLOSED"}
        self.github.extra_issues[dependency_number] = copy.deepcopy(dependency_issue)
        parent_plan = {"base_sha": self.sha, "jobs": []}
        parent_hash = content_hash(parent_plan)
        self.db.save_plan(42, parent_hash, parent_plan)
        self.db.mark_plan_needs_split(42, parent_hash)
        self.db.begin_issue_split(
            42,
            parent_hash,
            [
                {
                    "job_id": "contract-42",
                    "marker": "contract-marker",
                    "depends_on": [],
                },
                {
                    "job_id": "api-42",
                    "marker": "api-marker",
                    "depends_on": ["contract-42"],
                },
            ],
        )
        self.db.record_split_child_created(
            42, parent_hash, "contract-42", dependency_number, dependency_issue["url"]
        )
        self.db.record_split_child_created(
            42, parent_hash, "api-42", 12, self.issue["url"]
        )

    def launch_failed_review_repair(self):
        remote = self.root / "repair-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        (self.repo / "file.txt").write_text("initial implementation with a bug\n")
        (self.repo / WORKER_RESULT).write_text(
            json.dumps({"status": "ready", "summary": "initial implementation"})
        )
        self.write_launch_status("completed", exit_code=0)
        self.controller.run_once()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        branch = job["branch"]
        pr = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            head,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "## Smoke evidence\nreal output",
        )
        self.github.prs[branch] = pr
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "reviewing")
        self.github.verdicts[(5, head)] = "fail"
        self.github.feedbacks[(5, head)] = {
            "verdict": "fail",
            "body": "FAIL: the current response breaks the existing consumer schema.",
        }
        self.controller.run_once()
        return remote, pr, self.db.get_job("api-12")

    def test_no_approval_means_no_job_or_workspace(self):
        result = self.controller.run_once()
        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(self.db.list_jobs(), [])
        self.assertEqual(self.superset.worker_creates, 0)

    def test_split_dependency_is_injected_into_generated_plan(self):
        self.record_split_dependency()
        self.github.extra_issues[11]["state"] = "CLOSED"

        self.controller.run_once()

        current = self.db.current_plan(12)
        self.assertEqual(current["plan"]["issue_dependencies"], [11])
        self.assertEqual(current["plan_hash"], content_hash(current["plan"]))

    def test_split_dependency_barrier_retires_already_started_duplicate_work(self):
        self.record_split_dependency()
        issue = self.db.upsert_issue(self.issue)
        self.db.save_plan(12, self.plan_hash, self.plan)
        self.db.record_approval(
            12,
            self.plan_hash,
            "owner",
            "approval-1",
            "https://example/approval",
            "2026-01-01T00:00:00Z",
        )
        self.db.materialize_jobs(12, self.plan_hash, self.plan)
        self.db.acquire_lane("I", "api-12")
        self.db.update_job(
            "api-12",
            state="running",
            workspace_id="duplicate-workspace",
            pr_number=5,
            pr_url="https://example/pr/5",
            head_sha="b" * 40,
        )
        self.github.prs["lane-1/12-api"] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            "b" * 40,
            "RED",
            "BLOCKED",
            "MERGEABLE",
            False,
            "lane-1/12-api",
            "Controller job: `api-12` · lane `I`\n\n## Smoke evidence\nreal output",
        )

        current, jobs, halted = self.controller._prepare_plan_state(issue)

        self.assertTrue(halted)
        self.assertIsNone(current)
        self.assertEqual(jobs, [])
        self.assertEqual(self.db.get_job("api-12")["state"], "superseded")
        self.assertIsNone(self.db.lease_owner("I"))
        self.assertIsNone(self.db.current_plan(12))
        self.assertEqual(self.db.get_issue(12)["controller_state"], "approved")
        self.assertEqual(self.github.closed_prs[0][0], 5)
        self.assertIn("split dependency barrier", self.github.closed_prs[0][1])

        # A restart after the durable retirement but before GitHub closure retries
        # cleanup without reviving or accepting the superseded workspace.
        self.github.prs["lane-1/12-api"] = replace(
            self.github.prs["lane-1/12-api"], state="OPEN"
        )
        self.github.closed_prs.clear()
        self.controller._prepare_plan_state(self.db.get_issue(12))
        self.assertEqual(self.github.closed_prs[0][0], 5)

    def test_plan_publication_failure_preserves_the_valid_plan_for_retry(self):
        def fail_post(*_args, **_kwargs):
            raise RuntimeError("GitHub comment authority unavailable")

        self.github.post_plan = fail_post

        result = self.controller.run_once()

        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertIsNotNone(self.db.current_plan(12))
        self.assertEqual(result["errors"][0]["type"], "RuntimeError")
        self.assertNotEqual(self.db.get_issue(12)["controller_state"], "blocked")

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

    def test_dependent_job_records_current_main_as_its_immutable_launch_base(self):
        self.add_dependent_job()
        remote = self.root / "dependency-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "file.txt").write_text("merged dependency\n")
        subprocess.run(["git", "add", "file.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "dependency merged"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=self.repo, check=True, capture_output=True)
        merged_main = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        self.github.sha = merged_main
        self.db.upsert_issue(self.issue)
        self.db.save_plan(12, self.plan_hash, self.plan)
        self.db.record_approval(12, self.plan_hash, "owner", "1", None, "now")
        self.db.materialize_jobs(12, self.plan_hash, self.plan)

        child = self.controller._ensure_job_base_sha(self.plan, self.db.get_job("web-12"))

        self.assertEqual(child["base_sha"], merged_main)
        self.assertEqual(self.db.get_job("api-12")["base_sha"], self.sha)

    def test_issue_dependent_job_launches_from_current_main(self):
        remote = self.root / "issue-dependency-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "file.txt").write_text("external issue dependency merged\n")
        subprocess.run(["git", "add", "file.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "external dependency merged"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=self.repo, check=True, capture_output=True)
        merged_main = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        self.github.sha = merged_main
        plan = copy.deepcopy(self.plan)
        plan["issue_dependencies"] = [11]
        plan_hash = content_hash(plan)
        self.db.upsert_issue(self.issue)
        self.db.save_plan(12, plan_hash, plan)
        self.db.record_approval(12, plan_hash, "owner", "1", None, "now")
        self.db.materialize_jobs(12, plan_hash, plan)

        job = self.controller._ensure_job_base_sha(plan, self.db.get_job("api-12"))

        self.assertEqual(job["base_sha"], merged_main)

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

    def test_safe_scope_blocker_replans_with_new_identities_and_fresh_approval(self):
        first = self.controller.run_once()["issues"][0]["plan_hash"]
        self.approve()
        self.controller.run_once()
        self.db.update_job(
            "api-12",
            state="blocked",
            last_error=(
                "file.txt is not in the authoritative .lane allowed list; "
                "requires the controller to add it to this job's write scope"
            ),
        )

        result = self.controller.run_once()

        replacement = self.db.current_plan(12)
        self.assertIsNotNone(replacement)
        self.assertNotEqual(replacement["plan_hash"], first)
        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(replacement["plan"]["jobs"][0]["id"], "api-12-r2")
        self.assertEqual(replacement["plan"]["jobs"][0]["branch"], "lane-1/12-api-r2")
        self.assertEqual(self.db.get_job("api-12")["state"], "superseded")
        self.assertFalse(self.db.is_approved(first))
        self.assertFalse(self.db.is_approved(replacement["plan_hash"]))
        self.assertEqual(self.superset.worker_creates, 1)
        self.assertEqual(len(self.github.auto_replans), 1)
        self.assertEqual(self.github.auto_replans[0][2][0]["kind"], "lane_scope")

        self.controller.run_once()
        self.assertEqual(self.superset.worker_creates, 1)
        self.assertEqual(self.db.list_current_jobs(12), [])

        self.approve()
        self.controller.run_once()
        current_job = self.db.list_current_jobs(12)[0]
        self.assertEqual(current_job["job_id"], "api-12-r2")
        self.assertEqual(current_job["state"], "running")
        self.assertEqual(self.superset.worker_creates, 2)
        workspace_name = worker_workspace_name(
            "alfred-code", 12, "I", "api-12-r2"
        )
        self.assertEqual(
            self.superset.workspaces_by_name[workspace_name].branch,
            "lane-1/12-api-r2",
        )

    def test_ci_failure_never_auto_replans(self):
        first = self.controller.run_once()["issues"][0]["plan_hash"]
        self.approve()
        self.controller.run_once()
        head = "b" * 40
        self.db.update_job(
            "api-12",
            state="blocked",
            pr_number=41,
            pr_url="https://example/pr/41",
            head_sha=head,
            last_error="GitHub CI is red",
        )
        self.github.prs["lane-1/12-api"] = PullRequestObservation(
            41,
            "https://example/pr/41",
            "OPEN",
            head,
            "RED",
            "BLOCKED",
            "MERGEABLE",
            False,
            "lane-1/12-api",
            "Controller job: `api-12` · lane `I`\n\n## Smoke evidence\nreal",
        )

        result = self.controller.run_once()

        self.assertEqual(result["issues"][0]["state"], "blocked")
        self.assertEqual(self.db.current_plan(12)["plan_hash"], first)
        self.assertEqual(self.planner.calls, 1)
        self.assertEqual(self.github.auto_replans, [])
        self.assertEqual(self.github.closed_prs, [])

    def test_conflicting_controller_pr_is_closed_as_superseded_before_replan(self):
        first = self.controller.run_once()["issues"][0]["plan_hash"]
        self.approve()
        self.controller.run_once()
        head = "c" * 40
        self.db.update_job(
            "api-12",
            state="blocked",
            pr_number=42,
            pr_url="https://example/pr/42",
            head_sha=head,
            last_error="PR conflicts with its base",
        )
        self.github.prs["lane-1/12-api"] = PullRequestObservation(
            42,
            "https://example/pr/42",
            "OPEN",
            head,
            "GREEN",
            "DIRTY",
            "CONFLICTING",
            False,
            "lane-1/12-api",
            "Controller job: `api-12` · lane `I`\n\n## Smoke evidence\nreal",
        )

        result = self.controller.run_once()

        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertNotEqual(self.db.current_plan(12)["plan_hash"], first)
        self.assertEqual(self.db.get_job("api-12")["state"], "superseded")
        self.assertEqual([number for number, _ in self.github.closed_prs], [42])
        self.assertIn("branch and commits are retained", self.github.closed_prs[0][1])

    def test_foreign_conflicting_pr_is_never_closed_or_auto_replanned(self):
        first = self.controller.run_once()["issues"][0]["plan_hash"]
        self.approve()
        self.controller.run_once()
        head = "d" * 40
        self.db.update_job(
            "api-12",
            state="blocked",
            pr_number=43,
            pr_url="https://example/pr/43",
            head_sha=head,
            last_error="PR conflicts with its base",
        )
        self.github.prs["lane-1/12-api"] = PullRequestObservation(
            43,
            "https://example/pr/43",
            "OPEN",
            head,
            "GREEN",
            "DIRTY",
            "CONFLICTING",
            False,
            "lane-1/12-api",
            "An unrelated pull request",
        )

        self.controller.run_once()

        self.assertEqual(self.db.current_plan(12)["plan_hash"], first)
        self.assertEqual(self.planner.calls, 1)
        self.assertEqual(self.github.closed_prs, [])
        self.assertEqual(self.github.auto_replans, [])

    def test_auto_replan_attempt_cap_escalates_without_replanning(self):
        first = self.controller.run_once()["issues"][0]["plan_hash"]
        self.db.record_approval(12, first, "owner", "1", None, "now")
        self.db.materialize_jobs(12, first, self.plan)
        self.db.update_job(
            "api-12",
            state="blocked",
            last_error="No contract change is approved; contract requires X but acceptance requires Y",
        )
        self.controller.config = replace(self.config, auto_replan_max_attempts=0)

        self.controller.run_once()

        self.assertEqual(self.db.current_plan(12)["plan_hash"], first)
        self.assertEqual(self.planner.calls, 1)
        self.assertEqual(self.github.auto_replans, [])
        self.assertTrue(
            any("requires operator guidance" in message for message, _ in self.channel.messages)
        )

    def test_auto_replan_attempt_cap_resets_after_merged_progress(self):
        first = self.controller.run_once()["issues"][0]["plan_hash"]
        self.db.record_approval(12, first, "owner", "1", None, "now")
        self.db.materialize_jobs(12, first, self.plan)
        for suffix in ("old-a", "old-b"):
            self.db.event(
                "plan.auto_replan_requested",
                {"plan_hash": suffix},
                issue_number=12,
            )
        self.db.event(
            "job.transition",
            {"from": "ready_merge", "to": "merged"},
            issue_number=12,
        )
        self.db.update_job(
            "api-12",
            state="blocked",
            last_error="controller finalization failed: Scope limit exceeded: 214 LOC changed > 200 cap for lane I.",
        )

        result = self.controller.run_once()

        replacement = self.db.current_plan(12)
        self.assertIsNotNone(replacement)
        self.assertNotEqual(replacement["plan_hash"], first)
        self.assertEqual(result["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(self.db.get_job("api-12")["state"], "superseded")
        self.assertEqual(len(self.github.auto_replans), 1)

    def test_mixed_safe_and_unknown_blockers_do_not_replan(self):
        safe = {
            "job_id": "api-12",
            "lane": "I",
            "state": "blocked",
            "last_error": "not in the authoritative .lane allowed list",
        }
        unsafe = {
            "job_id": "web-12",
            "lane": "II",
            "state": "blocked",
            "last_error": "verification failed: regression detected",
        }

        self.assertIsNone(self.controller._auto_replan_blockers([safe, unsafe]))

    def test_scope_contract_and_declared_dependency_blockers_replan_together(self):
        blockers = [
            {
                "job_id": "ctrl-316",
                "lane": "I",
                "state": "blocked",
                "last_error": "controller finalization failed: Scope limit exceeded: 1238 LOC changed > 200 cap for lane I.",
            },
            {
                "job_id": "learn-316",
                "lane": "II",
                "state": "blocked",
                "last_error": "The requested scheduled janitor contract is absent; implementing it would require inventing an endpoint across lanes.",
            },
            {
                "job_id": "vault-316",
                "lane": "IV",
                "state": "blocked",
                "last_error": "Required full pytest cannot collect because declared dependencies are absent (numpy, structlog, and python-frontmatter).",
            },
        ]

        observed = self.controller._auto_replan_blockers(blockers)

        self.assertEqual(
            [item["kind"] for item in observed],
            ["scope_limit", "contract_plan", "dependency_environment"],
        )

    def test_generic_codex_exit_with_unsupported_scope_is_safely_replanned(self):
        blockers = [
            {
                "job_id": "ctrl-316",
                "lane": "I",
                "state": "blocked",
                "paths": ["packages/ctrl/src/api/routes/workers.ts"],
                "last_error": (
                    "controller finalization failed: Scope limit exceeded: "
                    "203 LOC changed > 200 cap for lane I."
                ),
            },
            {
                "job_id": "vault-316",
                "lane": "IV",
                "state": "blocked",
                "paths": [
                    "packages/alfred-vault/src/alfred/worker_runs.py",
                    "packages/alfred-vault/tests/test_worker_runs*.py",
                ],
                "last_error": (
                    "scoped agent launch exited: agent exited before writing "
                    "its required result marker (exit code 1)"
                ),
            },
            {
                "job_id": "learn-316",
                "lane": "II",
                "state": "waiting_dependency",
                "paths": ["packages/learn/src/activities/maintenance.py"],
                "last_error": None,
            },
        ]

        observed = self.controller._auto_replan_blockers(blockers)

        self.assertEqual(
            [item["kind"] for item in observed],
            ["scope_limit", "launch_scope_policy"],
        )
        self.assertIn("test_worker_runs*.py", observed[1]["reason"])

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

        self.assertEqual(result["issues"], [])
        self.assertEqual(self.planner.calls, 0)
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
        self.assertEqual(project.refreshes, [3])

    def test_historical_closed_issue_with_stale_plan_is_reconciled(self):
        self.controller.run_once()
        plan_hash = self.db.current_plan(12)["plan_hash"]
        self.github.issue_value.update(
            {
                "state": "CLOSED",
                "stateReason": "completed",
                "closedAt": "2026-08-19T09:00:00Z",
                "closedBy": "ssdavidai",
            }
        )
        self.db.upsert_issue(self.github.issue_value)
        self.assertEqual(self.db.get_issue(12)["controller_state"], "awaiting_approval")

        result = self.controller.run_once()

        self.assertEqual(result["issues"][0]["state"], "closed")
        issue = self.db.get_issue(12)
        self.assertEqual(issue["product_stage"], "done")
        self.assertIsNone(issue["current_plan_hash"])
        historical = self.db.connection.execute(
            "SELECT status FROM plans WHERE plan_hash=?", (plan_hash,)
        ).fetchone()
        self.assertEqual(historical["status"], "superseded")
        evidence = self.db.latest_event(12, "issue.closed_reconciled")["detail"]
        self.assertEqual(evidence["state_reason"], "completed")
        self.assertEqual(evidence["closed_by"], "ssdavidai")

    def test_reopened_terminal_issue_returns_to_clean_backlog(self):
        project = FakeProject()
        self.controller.project = project
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
        )
        self.controller.run_once()
        self.github.issue_value["state"] = "CLOSED"
        self.controller.run_once()
        self.assertEqual(self.db.get_issue(12)["controller_state"], "closed")

        self.github.issue_value["state"] = "OPEN"
        result = self.controller.run_once()

        self.assertEqual(result["issues"], [])
        issue = self.db.get_issue(12)
        self.assertEqual(issue["controller_state"], "observed")
        self.assertEqual(issue["product_stage"], "backlog")
        self.assertIsNone(issue["current_plan_hash"])

    def test_project_snapshot_refresh_is_bounded_and_recovers_after_backoff(self):
        project = FakeProject()
        self.controller.project = project
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
            project_refresh_seconds=900,
        )

        self.controller.observe_issues()
        self.controller.observe_issues()
        self.assertEqual(project.refreshes, [3])

        self.controller._project_refresh_attempted_at[3] -= 901
        self.controller.observe_issues()
        self.assertEqual(project.refreshes, [3, 3])

    def test_failed_project_refresh_is_not_retried_each_poll(self):
        project = FakeProject()
        project.refresh_error = AuthorityUnavailable("rate limit exceeded")
        self.controller.project = project
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
            project_refresh_seconds=900,
        )

        self.controller.observe_issues()
        self.controller.observe_issues()

        self.assertEqual(project.refreshes, [3])
        self.assertEqual(project.synced, {})

    def test_auto_intake_projects_unlabeled_issue_as_agent_silent_backlog(self):
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

        self.assertEqual(result["issues"], [])
        self.assertEqual(self.planner.calls, 0)
        projected_states = [
            item["controller_state"]
            for item in project.history
            if item["issue_url"] == self.issue["url"]
        ]
        self.assertEqual(projected_states, ["observed"])
        self.assertEqual(
            project.synced[self.issue["url"]]["product_stage"],
            "backlog",
        )

    def test_sprint_queue_is_silent_until_explicit_start_then_specifies(self):
        project = FakeProject()
        project.items = [
            {
                "issue_number": 12,
                "issue_url": self.issue["url"],
                "stage": "Sprint queue",
                "rank": 0,
            }
        ]
        self.controller.project = project
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
        )

        before = self.controller.run_once()

        self.assertEqual(before["issues"], [])
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.db.get_issue(12)["product_stage"], "sprint_queue")

        self.db.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[12],
        )
        after = self.controller.run_once()

        self.assertEqual(after["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(self.planner.calls, 1)
        self.assertEqual(self.db.get_issue(12)["product_stage"], "active")

    def test_twenty_one_point_plan_is_not_executable_and_closes_sprint_for_refinement(self):
        self.db.upsert_issue(self.issue)
        self.db.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[12],
        )
        self.plan.update(
            {
                "story_points": 21,
                "points_evidence": "Multiple lanes and contracts exceed one bounded delivery.",
                "issue_dependencies": [],
            }
        )
        self.plan_hash = content_hash(self.plan)
        self.planner.plan = copy.deepcopy(self.plan)
        self.planner.plan_hash = self.plan_hash

        result = self.controller.run_once()

        self.assertIsNone(self.db.active_sprint())
        self.assertEqual(self.db.current_plan(12)["status"], "needs_split")
        self.assertEqual(self.db.get_issue(12)["product_stage"], "needs_split")
        self.assertIsNone(self.db.issue_split(12))
        self.assertEqual(self.superset.worker_creates, 0)
        self.assertEqual(result["sprint"]["state"], "closed")

    def test_blocked_sprint_item_replans_in_inbox_but_cannot_build_until_reselected(self):
        project = FakeProject()
        self.controller.project = project
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
        )
        self.db.upsert_issue(self.issue)
        self.db.set_product_stage(12, "sprint_queue")
        first_sprint = self.db.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[12],
        )
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        head = "b" * 40
        self.db.update_job(
            "api-12",
            state="blocked",
            pr_number=55,
            pr_url="https://example/pr/55",
            head_sha=head,
            last_error="independent review found a contract mismatch",
        )
        self.db.set_issue_state(12, "blocked")
        self.github.prs[job["branch"]] = PullRequestObservation(
            55,
            "https://example/pr/55",
            "OPEN",
            head,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            job["branch"],
            "Controller job: `api-12` · lane `I`",
        )

        closed = self.controller._reconcile_active_sprint()

        self.assertEqual(closed["state"], "closed")
        self.assertEqual(self.db.sprint_items(first_sprint["id"])[0]["status"], "blocked")
        self.assertEqual(self.github.closed_prs[0][0], 55)
        self.assertIn("Inbox replanning", self.github.closed_prs[0][1])
        carried = self.db.get_issue(12)
        self.assertEqual(carried["product_stage"], "inbox")
        self.assertEqual(carried["carryover_replan"], 1)
        self.assertIsNone(self.db.current_plan(12))
        self.assertEqual(self.db.get_job("api-12")["state"], "superseded")
        prepared = self.db.latest_event(12, "sprint.carryover_prepared")
        self.assertEqual(prepared["detail"]["pull_requests"], [55])

        replanned = self.controller.run_once()

        self.assertEqual(self.planner.calls, 2)
        self.assertEqual(replanned["issues"][0]["state"], "awaiting_approval")
        self.assertEqual(replanned["issues"][0]["product_stage"], "inbox")
        self.assertEqual(self.db.get_issue(12)["carryover_replan"], 1)
        self.assertEqual(self.db.list_current_jobs(12), [])
        self.assertEqual(self.superset.worker_creates, 1)

        self.approve()
        approved = self.controller.run_once()

        self.assertEqual(approved["issues"][0]["state"], "approved")
        self.assertEqual(approved["issues"][0]["product_stage"], "inbox")
        self.assertEqual(self.db.get_issue(12)["carryover_replan"], 0)
        self.assertEqual(self.db.list_current_jobs(12), [])
        self.assertEqual(self.superset.worker_creates, 1)
        self.assertEqual(self.controller.run_once()["issues"], [])

        project.items = [
            {
                "issue_number": 12,
                "issue_url": self.issue["url"],
                "stage": "Sprint queue",
                "rank": 0,
            }
        ]
        self.controller.run_once()
        self.assertEqual(self.db.get_issue(12)["product_stage"], "sprint_queue")
        self.assertEqual(self.db.list_current_jobs(12), [])
        self.db.start_sprint(
            title="Sprint 1",
            duration_days=14,
            starts_at="2026-08-05T08:00:00Z",
            ends_at="2026-08-19T08:00:00Z",
            iteration_id="iteration-1",
            issue_numbers=[12],
        )

        launched = self.controller.run_once()

        self.assertEqual(launched["issues"][0]["state"], "building")
        self.assertEqual(self.db.list_current_jobs(12)[0]["job_id"], "api-12-r2")
        self.assertEqual(self.superset.worker_creates, 2)

    def test_unowned_open_pr_prevents_blocked_sprint_carryover(self):
        self.controller.project = FakeProject()
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
        )
        self.db.upsert_issue(self.issue)
        self.db.set_product_stage(12, "sprint_queue")
        self.db.start_sprint(
            title="Sprint 0 — Calibration",
            duration_days=14,
            starts_at="2026-07-22T08:00:00Z",
            ends_at="2026-08-05T08:00:00Z",
            iteration_id="iteration-0",
            issue_numbers=[12],
        )
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        head = "c" * 40
        self.db.update_job(
            "api-12",
            state="blocked",
            pr_number=56,
            pr_url="https://example/pr/56",
            head_sha=head,
            last_error="unrecognized terminal blocker",
        )
        self.db.set_issue_state(12, "blocked")
        self.github.prs[job["branch"]] = PullRequestObservation(
            56,
            "https://example/pr/56",
            "OPEN",
            head,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            job["branch"],
            "A human-authored PR without the controller ownership marker.",
        )

        with self.assertRaisesRegex(AuthorityUnavailable, "is not owned"):
            self.controller._reconcile_active_sprint()

        self.assertIsNotNone(self.db.active_sprint())
        self.assertEqual(self.github.closed_prs, [])
        self.assertEqual(self.db.get_issue(12)["product_stage"], "active")
        self.assertEqual(self.db.get_issue(12)["carryover_replan"], 0)
        self.assertEqual(self.db.current_plan(12)["plan_hash"], self.plan_hash)
        self.assertEqual(self.db.get_job("api-12")["state"], "blocked")
        self.assertIsNone(self.db.latest_event(12, "sprint.carryover_prepared"))

    def test_failed_inbox_carryover_plan_does_not_retry_forever(self):
        self.controller.project = FakeProject()
        self.controller.config = replace(
            self.config,
            github=replace(self.config.github, project_number=3),
        )
        self.db.upsert_issue(self.issue)
        self.db.connection.execute(
            "UPDATE issues SET product_stage='inbox', controller_state='planning', "
            "carryover_replan=1 WHERE number=12"
        )

        def fail_plan(_prepared):
            self.planner.calls += 1
            raise RuntimeError("planner unavailable")

        self.planner.execute_plan = fail_plan

        failed = self.controller.run_once()
        quiet = self.controller.run_once()

        self.assertEqual(failed["errors"][0]["error"], "planner unavailable")
        self.assertEqual(self.db.get_issue(12)["controller_state"], "blocked")
        self.assertEqual(self.db.get_issue(12)["product_stage"], "inbox")
        self.assertEqual(self.db.get_issue(12)["carryover_replan"], 0)
        self.assertEqual(quiet["issues"], [])
        self.assertEqual(self.planner.calls, 1)

    def test_approved_issue_waits_for_cross_issue_dependency_before_materializing_jobs(self):
        remote = self.root / "cross-issue-dependency-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=self.repo, check=True, capture_output=True)
        dependency = {
            "id": "I_13",
            "number": 13,
            "title": "Required contract",
            "body": "Ship first",
            "state": "OPEN",
            "url": "https://example/issues/13",
            "labels": [],
        }
        self.github.extra_issues[13] = dependency
        self.plan.update(
            {
                "story_points": 5,
                "points_evidence": "One lane waiting on an external contract issue.",
                "issue_dependencies": [13],
            }
        )
        self.plan_hash = content_hash(self.plan)
        self.planner.plan = copy.deepcopy(self.plan)
        self.planner.plan_hash = self.plan_hash
        self.controller.run_once()
        self.approve()

        waiting = self.controller.run_once()

        self.assertEqual(waiting["issues"][0]["state"], "approved")
        self.assertEqual(self.db.list_current_jobs(12), [])
        self.assertEqual(self.superset.worker_creates, 0)

        self.github.extra_issues[13]["state"] = "CLOSED"
        launched = self.controller.run_once()

        self.assertEqual(launched["issues"][0]["state"], "building")
        self.assertEqual(self.superset.worker_creates, 1)

    def test_planning_pool_is_parallel_but_respects_its_configured_bound(self):
        bodies = {12: self.issue["body"]}
        for number in range(13, 17):
            body = f"Build feature {number}"
            bodies[number] = body
            self.github.extra_issues[number] = {
                "id": f"I_{number}",
                "number": number,
                "title": f"Feature {number}",
                "body": body,
                "state": "OPEN",
                "url": f"https://example/issues/{number}",
                "labels": [{"name": "alfred-code"}],
            }
        planner = ConcurrentPlanner(self.sha, bodies, expected_parallel=2)
        self.controller.planner = planner
        self.controller.config = replace(self.config, max_parallel_planners=2)

        result = self.controller.run_once()

        self.assertEqual(planner.maximum_active, 2)
        self.assertEqual(sorted(planner.calls), [12, 13, 14, 15, 16])
        self.assertEqual([item["number"] for item in result["issues"]], [12, 13, 14, 15, 16])
        self.assertTrue(all(item["state"] == "awaiting_approval" for item in result["issues"]))

    def test_active_jobs_reconcile_while_planning_backlog_is_still_running(self):
        self.controller.run_once()
        body = "Build feature 13"
        self.github.extra_issues[13] = {
            "id": "I_13",
            "number": 13,
            "title": "Feature 13",
            "body": body,
            "state": "OPEN",
            "url": "https://example/issues/13",
            "labels": [{"name": "alfred-code"}],
        }
        planner = BlockingPlanner(self.sha, {13: body})
        self.controller.planner = planner
        self.controller._planning_reconcile_interval = lambda: 0.02

        initial_reconcile_done = threading.Event()
        process_issue_into = self.controller._process_issue_into

        def observe_reconcile(*args, **kwargs):
            process_issue_into(*args, **kwargs)
            if args[2] == 12:
                initial_reconcile_done.set()

        self.controller._process_issue_into = observe_reconcile
        reconciled_before_planner_exit = threading.Event()

        def approve_during_planning():
            if not planner.started.wait(timeout=1) or not initial_reconcile_done.wait(timeout=1):
                planner.release.set()
                return
            self.approve()
            deadline = time.monotonic() + 1
            while self.superset.worker_creates == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            if self.superset.worker_creates == 1:
                reconciled_before_planner_exit.set()
            planner.release.set()

        thread = threading.Thread(target=approve_during_planning)
        thread.start()
        self.controller.run_once()
        thread.join(timeout=2)

        self.assertTrue(planner.started.is_set())
        self.assertEqual(self.superset.worker_creates, 1)
        self.assertTrue(reconciled_before_planner_exit.is_set())
        self.assertFalse(thread.is_alive())

    def test_approval_launches_once_and_restart_adopts_workspace(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.assertEqual(job["state"], "running")
        self.assertEqual(self.superset.worker_creates, 1)
        self.controller.run_once()
        self.assertEqual(self.superset.worker_creates, 1)

    def test_running_workspace_reacquires_a_missing_lane_lease(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        self.db.release_lane("api-12")
        self.assertIsNone(self.db.lease_owner("I"))

        self.controller.run_once()

        self.assertEqual(self.db.get_job("api-12")["state"], "running")
        self.assertEqual(self.db.lease_owner("I"), "api-12")

    def test_open_pr_observation_stays_current_while_its_lane_is_busy(self):
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        self.db.release_lane("api-12")

        issue_13 = {
            "id": "I_13",
            "number": 13,
            "title": "Lane owner",
            "body": "Hold the lane",
            "state": "OPEN",
            "url": "https://example/issues/13",
            "labels": [{"name": "alfred-code"}],
        }
        plan_13 = copy.deepcopy(self.plan)
        plan_13["issue"] = 13
        plan_13["issue_body_hash"] = content_hash(issue_13["body"])
        plan_13["jobs"][0]["id"] = "api-13"
        plan_13["jobs"][0]["branch"] = "lane-1/13-api"
        plan_hash_13 = content_hash(plan_13)
        self.db.upsert_issue(issue_13)
        self.db.save_plan(13, plan_hash_13, plan_13)
        self.db.record_approval(13, plan_hash_13, "owner", "13", None, "now")
        self.db.materialize_jobs(13, plan_hash_13, plan_13)
        self.assertTrue(self.db.acquire_lane("I", "api-13"))

        branch = "lane-1/12-api"
        head_sha = "d" * 40
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            head_sha,
            "PENDING",
            "BLOCKED",
            "MERGEABLE",
            False,
            branch,
            "## Smoke evidence\nreal output",
        )

        self.controller.reconcile_job(
            self.db.get_issue(12),
            self.plan,
            self.db.get_job("api-12"),
        )

        waiting = self.db.get_job("api-12")
        self.assertEqual(waiting["state"], "waiting_lane")
        self.assertEqual(waiting["head_sha"], head_sha)
        self.assertEqual(waiting["pr_number"], 5)
        self.assertEqual(self.db.lease_owner("I"), "api-13")

        self.db.release_lane("api-13")
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            head_sha,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "## Smoke evidence\nreal output",
        )
        self.controller.reconcile_job(
            self.db.get_issue(12),
            self.plan,
            self.db.get_job("api-12"),
        )

        reviewing = self.db.get_job("api-12")
        self.assertEqual(reviewing["state"], "reviewing")
        self.assertIsNone(reviewing["last_error"])

    def test_independent_lanes_launch_workers_in_the_same_cycle(self):
        self.plan["jobs"].append(
            {
                "id": "web-12",
                "lane": "II",
                "title": "Web",
                "branch": "lane-2/12-web",
                "paths": ["web.txt"],
                "verify": "true",
                "contracts_read": [],
                "contracts_changed": [],
                "depends_on": [],
                "acceptance": ["works independently"],
            }
        )
        self.plan_hash = content_hash(self.plan)
        self.planner.plan_hash = self.plan_hash
        self.controller.run_once()
        self.approve()

        self.controller.run_once()

        self.assertEqual(self.superset.worker_creates, 2)
        self.assertEqual(self.db.get_job("api-12")["state"], "running")
        self.assertEqual(self.db.get_job("web-12")["state"], "running")
        self.assertEqual(self.db.lease_owner("I"), "api-12")
        self.assertEqual(self.db.lease_owner("II"), "web-12")

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
        self.db.update_job(
            "api-12",
            state="blocked",
            last_error=(
                "packages/ctrl/node_modules is a self-referential symlink, "
                "so npm run build cannot resolve esbuild"
            ),
        )
        self.db.release_lane("api-12")

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
        self.assertTrue(self.github.created_prs[0]["body"].startswith("Closes #12\n"))
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

    def test_multi_job_pull_request_does_not_close_parent_issue(self):
        self.add_dependent_job()
        remote = self.root / "multi-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        (self.repo / "file.txt").write_text("implemented first lane\n")
        (self.repo / WORKER_RESULT).write_text(json.dumps({"status": "ready", "summary": "done"}))
        self.write_launch_status("completed", exit_code=0)

        self.controller.run_once()

        self.assertEqual(self.db.get_job("api-12")["state"], "pr_open")
        self.assertTrue(self.github.created_prs[0]["body"].startswith("Part of #12\n"))
        self.assertNotIn("Closes #12", self.github.created_prs[0]["body"])

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

    def test_red_ci_starts_scoped_repair_without_releasing_lane(self):
        remote = self.root / "ci-repair-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        job = self.db.get_job("api-12")
        self.superset.details[job["workspace_id"]]["worktreePath"] = str(self.repo)
        subprocess.run(["git", "checkout", job["branch"]], cwd=self.repo, check=True, capture_output=True)
        self.write_worker_lane()
        (self.repo / "file.txt").write_text("implementation with a scanner false positive\n")
        (self.repo / WORKER_RESULT).write_text(
            json.dumps({"status": "ready", "summary": "initial implementation"})
        )
        self.write_launch_status("completed", exit_code=0)
        self.controller.run_once()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.github.prs[job["branch"]] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "OPEN",
            head,
            "RED",
            "BLOCKED",
            "MERGEABLE",
            False,
            job["branch"],
            "## Smoke evidence\nreal output",
            checks=(
                {
                    "name": "gitleaks",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/123/job/456",
                },
            ),
        )

        self.controller.run_once()

        repairing = self.db.get_job("api-12")
        self.assertEqual(repairing["state"], "repairing")
        self.assertEqual(repairing["repair_attempts"], 1)
        self.assertEqual(self.db.lease_owner("I"), "api-12")
        self.assertEqual(self.superset.agent_starts, 1)
        self.assertIn("gitleaks failed at docs/contract.md:234", self.superset.agent_prompts[0])
        self.assertIn("untrusted validation evidence", self.superset.agent_prompts[0])

        (self.repo / "file.txt").write_text("scanner-safe final implementation\n")
        (self.repo / WORKER_RESULT).write_text(
            json.dumps(
                {
                    "status": "ready",
                    "summary": "replaced the fake credential example",
                    "head_sha": head,
                    "handoff_token": repairing["repair_token"],
                    "attempt": 1,
                }
            )
        )
        self.write_launch_status(
            "completed",
            exit_code=0,
            mode="repair",
            head_sha=head,
            attempt=1,
        )

        self.controller.run_once()

        replaced = self.db.get_job("api-12")
        self.assertEqual(replaced["state"], "pr_open")
        self.assertEqual(replaced["branch"], "lane-1/12-api-clean-r1")
        self.assertEqual(self.github.created_prs[-1]["branch"], replaced["branch"])
        self.assertEqual(self.github.closed_prs[-1][0], 5)
        self.assertIn("no force-push", self.github.closed_prs[-1][1])
        replacement_count = subprocess.run(
            [
                "git",
                "--git-dir",
                str(remote),
                "rev-list",
                "--count",
                f"{self.sha}..refs/heads/{replaced['branch']}",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(replacement_count, "1")

    def test_failed_review_launches_bound_repair_and_re_reviews_new_sha(self):
        remote, pr, repairing = self.launch_failed_review_repair()

        self.assertEqual(repairing["state"], "repairing")
        self.assertEqual(repairing["repair_attempts"], 1)
        self.assertEqual(repairing["repair_sha"], pr.head_sha)
        lane = json.loads((self.repo / ".lane").read_text())
        self.assertEqual(lane["mode"], "repair")
        self.assertEqual(lane["head_sha"], pr.head_sha)
        self.assertEqual(lane["handoff_token"], repairing["repair_token"])
        self.assertEqual(self.superset.agent_starts, 1)

        (self.repo / "file.txt").write_text("compatible repaired implementation\n")
        (self.repo / WORKER_RESULT).write_text(
            json.dumps(
                {
                    "status": "ready",
                    "summary": "preserved the consumer schema",
                    "head_sha": pr.head_sha,
                    "handoff_token": repairing["repair_token"],
                    "attempt": 1,
                }
            )
        )
        self.write_launch_status(
            "completed",
            exit_code=0,
            mode="repair",
            head_sha=pr.head_sha,
            attempt=1,
        )

        self.controller.run_once()

        repaired = self.db.get_job("api-12")
        self.assertEqual(repaired["state"], "pr_open")
        self.assertNotEqual(repaired["head_sha"], pr.head_sha)
        remote_sha = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/lane-1/12-api"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(remote_sha, repaired["head_sha"])
        self.assertEqual(self.github.invalidated_prs, [pr.branch])
        repair_events = [event for event in self.db.events() if event["kind"] == "job.repair_pushed"]
        self.assertEqual(repair_events[0]["detail"]["from_sha"], pr.head_sha)
        self.assertEqual(repair_events[0]["detail"]["to_sha"], repaired["head_sha"])

        new_pr = replace(pr, head_sha=repaired["head_sha"])
        self.github.prs[pr.branch] = new_pr
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "reviewing")
        self.assertEqual(self.superset.review_creates, 2)
        self.github.verdicts[(5, new_pr.head_sha)] = "pass"
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "ready_merge")

    def test_stale_repair_result_is_quarantined(self):
        _, pr, repairing = self.launch_failed_review_repair()
        (self.repo / WORKER_RESULT).write_text(
            json.dumps(
                {
                    "status": "ready",
                    "summary": "stale marker without the controller token",
                    "head_sha": pr.head_sha,
                    "attempt": 1,
                }
            )
        )
        self.write_launch_status(
            "completed",
            exit_code=0,
            mode="repair",
            head_sha=pr.head_sha,
            attempt=1,
        )

        self.controller.run_once()

        quarantined = self.db.get_job("api-12")
        self.assertEqual(quarantined["state"], "quarantined")
        self.assertIn("not bound", quarantined["last_error"])
        self.assertIsNone(self.db.lease_owner("I"))
        self.assertTrue(repairing["repair_token"])

    def test_failed_provider_resumes_partial_repair_with_a_new_bound_attempt(self):
        remote, pr, repairing = self.launch_failed_review_repair()
        first_token = repairing["repair_token"]
        (self.repo / "file.txt").write_text("partial compatible repair\n")
        self.write_launch_status(
            "exited",
            exit_code=1,
            reason="provider was at capacity",
            mode="repair",
            head_sha=pr.head_sha,
            attempt=1,
        )

        self.controller.run_once()

        resumed = self.db.get_job("api-12")
        self.assertEqual(resumed["state"], "repairing")
        self.assertEqual(resumed["repair_attempts"], 2)
        self.assertNotEqual(resumed["repair_token"], first_token)
        self.assertEqual((self.repo / "file.txt").read_text(), "partial compatible repair\n")
        lane = json.loads((self.repo / ".lane").read_text())
        result = json.loads((self.repo / WORKER_RESULT).read_text())
        launch = json.loads((self.repo / LAUNCH_STATUS).read_text())
        for marker in (lane, result, launch):
            self.assertEqual(marker["head_sha"], pr.head_sha)
            self.assertEqual(marker["attempt"], 2)
        self.assertEqual(lane["handoff_token"], resumed["repair_token"])
        self.assertEqual(result["handoff_token"], resumed["repair_token"])
        self.assertEqual(launch["status"], "retrying")
        self.assertEqual(self.superset.agent_starts, 2)

        (self.repo / WORKER_RESULT).write_text(
            json.dumps(
                {
                    "status": "ready",
                    "summary": "finished the resumed repair",
                    "head_sha": pr.head_sha,
                    "handoff_token": resumed["repair_token"],
                    "attempt": 2,
                }
            )
        )
        self.write_launch_status(
            "completed",
            exit_code=0,
            mode="repair",
            head_sha=pr.head_sha,
            attempt=2,
        )

        self.controller.run_once()

        finalized = self.db.get_job("api-12")
        self.assertEqual(finalized["state"], "pr_open")
        self.assertNotEqual(finalized["head_sha"], pr.head_sha)
        remote_sha = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/lane-1/12-api"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(remote_sha, finalized["head_sha"])

    def test_revision_21_unbound_failure_recovers_only_bound_partial_repair(self):
        _, pr, repairing = self.launch_failed_review_repair()
        first_token = repairing["repair_token"]
        (self.repo / "file.txt").write_text("partial repair from interrupted provider\n")
        (self.repo / LAUNCH_STATUS).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "revision": LAUNCH_REVISION - 1,
                    "status": "failed",
                    "exit_code": 1,
                    "reason": "scoped launcher could not start a Python 3.11+ security runtime",
                }
            )
        )
        self.db.update_job(
            "api-12",
            state="quarantined",
            last_error="review repair launch marker is not bound to the current exact SHA and attempt",
        )
        self.db.release_lane("api-12")

        self.controller.run_once()

        resumed = self.db.get_job("api-12")
        self.assertEqual(resumed["state"], "repairing")
        self.assertEqual(resumed["repair_attempts"], 2)
        self.assertNotEqual(resumed["repair_token"], first_token)
        self.assertEqual(
            (self.repo / "file.txt").read_text(),
            "partial repair from interrupted provider\n",
        )
        self.assertEqual(self.db.lease_owner("I"), "api-12")
        self.assertEqual(self.superset.agent_starts, 2)

    def test_blocked_repair_stops_at_configured_attempt_cap(self):
        self.controller.config = replace(
            self.config,
            superset=replace(self.config.superset, review_repair_max_attempts=1),
        )
        _, pr, repairing = self.launch_failed_review_repair()
        (self.repo / WORKER_RESULT).write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "the contract requires a different lane",
                    "head_sha": pr.head_sha,
                    "handoff_token": repairing["repair_token"],
                    "attempt": 1,
                }
            )
        )
        self.write_launch_status(
            "completed",
            exit_code=0,
            mode="repair",
            head_sha=pr.head_sha,
            attempt=1,
        )
        self.controller.run_once()
        self.assertEqual(self.db.get_job("api-12")["state"], "blocked")

        self.controller.run_once()

        capped = self.db.get_job("api-12")
        self.assertEqual(capped["state"], "blocked")
        self.assertEqual(capped["repair_attempts"], 1)
        self.assertIn("operator attention", capped["last_error"])
        self.assertEqual(self.superset.agent_starts, 1)

    def test_capped_historical_gitleaks_repair_recovers_after_restart(self):
        self.controller.config = replace(
            self.config,
            superset=replace(self.config.superset, review_repair_max_attempts=1),
        )
        remote, pr, repairing = self.launch_failed_review_repair()
        self.github.prs[repairing["branch"]] = replace(
            pr,
            ci="RED",
            checks=(
                {
                    "name": "gitleaks",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/123/job/456",
                },
            ),
        )
        self.github.failure_evidence = (
            "Finding: Generic API Key\n"
            f"Commit: {self.sha}\n"
            "File: file.txt\n"
        )
        (self.repo / WORKER_RESULT).write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "the historical finding requires a squash onto the approved base",
                    "head_sha": pr.head_sha,
                    "handoff_token": repairing["repair_token"],
                    "attempt": 1,
                }
            )
        )
        self.write_launch_status(
            "completed",
            exit_code=0,
            mode="repair",
            head_sha=pr.head_sha,
            attempt=1,
        )
        self.db.update_job(
            "api-12",
            state="blocked",
            last_error=(
                "automated validation failed after 1 bounded repair attempt(s); "
                "operator attention is required"
            ),
        )
        self.db.release_lane("api-12")

        self.controller.run_once()

        recovered = self.db.get_job("api-12")
        self.assertEqual(recovered["state"], "pr_open")
        self.assertEqual(recovered["branch"], "lane-1/12-api-clean-r1")
        self.assertEqual(self.github.created_prs[-1]["branch"], recovered["branch"])
        self.assertEqual(self.github.closed_prs[-1][0], 5)
        replacement_count = subprocess.run(
            [
                "git",
                "--git-dir",
                str(remote),
                "rev-list",
                "--count",
                f"{self.sha}..refs/heads/{recovered['branch']}",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(replacement_count, "1")
        self.assertTrue(
            any(
                event["kind"] == "job.clean_history_recovery_started"
                for event in self.db.events(20)
            )
        )

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

    def test_controller_generated_multi_job_auto_close_is_reopened_and_resumed(self):
        self.add_dependent_job()
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        merged_at = "2026-07-17T20:16:35Z"
        branch = "lane-1/12-api"
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "MERGED",
            "b" * 40,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "Closes #12\n\nController job: `api-12` · lane `I`\n\n## Smoke evidence\nreal",
            merged_at,
        )
        self.github.issue_value["state"] = "CLOSED"
        self.github.issue_value["updatedAt"] = "2026-07-17T20:16:36Z"

        result = self.controller.run_once()

        self.assertEqual(self.github.reopened_issues, [12])
        self.assertEqual(self.github.updated_pr_bodies[0][0], 5)
        self.assertTrue(self.github.updated_pr_bodies[0][1].startswith("Part of #12\n"))
        self.assertEqual(self.github.issue_value["state"], "OPEN")
        self.assertEqual(self.db.get_job("api-12")["state"], "merged")
        self.assertEqual(self.db.get_job("web-12")["state"], "waiting_dependency")
        self.assertEqual(result["issues"][0]["state"], "building")

    def test_recovery_retries_a_github_reclose_from_the_pre_neutralization_release(self):
        self.add_dependent_job()
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "MERGED",
            "b" * 40,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "Closes #12\n\nController job: `api-12` · lane `I`",
            "2026-07-17T20:16:35Z",
        )
        self.db.update_job("api-12", state="merged")
        self.db.update_job(
            "web-12",
            state="closed",
            last_error=(
                "GitHub issue or PR closed without merge "
                "(automatic multi-job close recovery not applicable)"
            ),
        )
        self.db.event("issue.premature_close_recovered", {}, issue_number=12)
        recovered_at = self.db.latest_event(12, "issue.premature_close_recovered")["created_at"]
        self.github.issue_value["state"] = "CLOSED"
        self.github.issue_value["updatedAt"] = recovered_at
        self.db.upsert_issue(self.github.issue_value)
        self.db.set_issue_state(12, "closed")

        result = self.controller.run_once()

        self.assertEqual(self.github.reopened_issues, [12])
        self.assertEqual(self.github.updated_pr_bodies[0][0], 5)
        self.assertNotIn("Closes #12", self.github.updated_pr_bodies[0][1])
        self.assertEqual(self.db.get_job("web-12")["state"], "waiting_dependency")
        self.assertEqual(result["issues"][0]["state"], "building")

    def test_operator_close_is_not_reopened_even_with_an_older_controller_pr(self):
        self.add_dependent_job()
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "MERGED",
            "b" * 40,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "Closes #12\n\nController job: `api-12` · lane `I`",
            "2026-07-17T20:00:00Z",
        )
        self.github.issue_value["state"] = "CLOSED"
        self.github.issue_value["updatedAt"] = "2026-07-17T20:16:36Z"

        result = self.controller.run_once()

        self.assertEqual(self.github.reopened_issues, [])
        self.assertEqual(result["issues"][0]["state"], "closed")
        self.assertEqual(self.db.get_job("web-12")["state"], "closed")

    def test_historical_close_backfill_never_reopens_or_mutates_pull_requests(self):
        self.add_dependent_job()
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        branch = "lane-1/12-api"
        merged_at = "2026-07-17T20:16:35Z"
        self.github.prs[branch] = PullRequestObservation(
            5,
            "https://example/pr/5",
            "MERGED",
            "b" * 40,
            "GREEN",
            "CLEAN",
            "MERGEABLE",
            False,
            branch,
            "Closes #12\n\nController job: `api-12` · lane `I`",
            merged_at,
        )
        self.github.issue_value["state"] = "CLOSED"
        self.github.issue_value["updatedAt"] = merged_at
        self.db.upsert_issue(self.github.issue_value)

        result = self.controller.run_once()

        self.assertEqual(self.github.reopened_issues, [])
        self.assertEqual(self.github.updated_pr_bodies, [])
        self.assertEqual(result["issues"][0]["state"], "closed")
        self.assertEqual(self.db.get_issue(12)["product_stage"], "done")

    def test_multi_job_issue_closes_only_after_every_job_is_merged(self):
        self.add_dependent_job()
        self.controller.run_once()
        self.approve()
        self.controller.run_once()
        first = "lane-1/12-api"
        second = "lane-2/12-web"
        self.github.prs[first] = PullRequestObservation(
            5, "https://example/pr/5", "MERGED", "a" * 40, "GREEN", "CLEAN", "MERGEABLE", False, first,
            "Part of #12\n\nController job: `api-12` · lane `I`",
        )
        self.db.update_job("web-12", state="running")
        self.github.prs[second] = PullRequestObservation(
            6, "https://example/pr/6", "MERGED", "b" * 40, "GREEN", "CLEAN", "MERGEABLE", False, second,
            "Part of #12\n\nController job: `web-12` · lane `II`",
        )

        result = self.controller.run_once()

        self.assertEqual(self.github.closed_issues, [12])
        self.assertEqual(self.github.issue_value["state"], "CLOSED")
        self.assertEqual(result["issues"][0]["state"], "completed")
        self.assertEqual(result["issues"][0]["github_state"], "CLOSED")

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
