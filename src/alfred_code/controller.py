from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_security import REVIEW_RESULT, SECURITY_POLICY, WORKER_RESULT
from .audit import AuditLog
from .config import ControllerConfig
from .db import Database
from .errors import AlfredCodeError, AuthorityUnavailable, CommandError, PlanValidationError
from .github import GitHubClient, PullRequestObservation
from .notify import DurableNotifier
from .planner import Planner
from .plans import path_matches
from .project import ProjectBoard
from .states import TERMINAL_JOB_STATES
from .superset import SupersetClient
from .util import run, utcnow


class Controller:
    def __init__(
        self,
        config: ControllerConfig,
        database: Database,
        github: GitHubClient,
        superset: SupersetClient,
        planner: Planner,
        notifier: DurableNotifier,
        *,
        project: ProjectBoard | None = None,
        audit: AuditLog | None = None,
    ):
        self.config = config
        self.database = database
        self.github = github
        self.superset = superset
        self.planner = planner
        self.notifier = notifier
        self.project = project
        self.audit = audit or AuditLog(config.state_dir / "controller.jsonl")

    def _record(self, kind: str, **detail: Any) -> None:
        self.audit.write(kind, **detail)

    def observe_issues(self) -> list[dict[str, Any]]:
        open_issues = {int(issue["number"]): issue for issue in self.github.open_issues()}
        tracked = {int(issue["number"]): issue for issue in self.database.list_issues()}
        live = dict(open_issues)
        for number, issue in tracked.items():
            if issue["github_state"] == "OPEN" and number not in live:
                live[number] = self.github.issue(number)

        project_ready = bool(
            self.config.apply and self.project and self.config.github.project_number
        )
        if project_ready:
            try:
                self.project.refresh(int(self.config.github.project_number))
            except AuthorityUnavailable as exc:
                project_ready = False
                self._record("project.refresh_failed", error=str(exc))

        candidates: list[dict[str, Any]] = []
        intake_label = self.config.github.intake_label
        for number in sorted(live):
            previous = tracked.get(number)
            local = self.database.upsert_issue(live[number])
            self.database.observe("github", f"issue:{number}", live[number])
            jobs = self.database.list_jobs(number)
            current = self.database.current_plan(number)

            if (
                previous
                and previous["github_state"] == "CLOSED"
                and local["github_state"] == "OPEN"
                and local["controller_state"] == "closed"
                and not current
                and not jobs
            ):
                self.database.set_issue_state(number, "observed", {"reason": "GitHub issue reopened"})
                local = self.database.get_issue(number) or local
            elif local["github_state"] == "CLOSED" and local["controller_state"] == "observed":
                self.database.set_issue_state(number, "closed", {"reason": "GitHub issue closed"})
                local = self.database.get_issue(number) or local

            labels = {
                str(label.get("name") or "")
                for label in live[number].get("labels", [])
                if isinstance(label, dict)
            }
            enrolled = local["github_state"] == "OPEN" and (
                self.config.github.auto_intake
                or not intake_label
                or intake_label in labels
            )
            if (
                self.config.apply
                and enrolled
                and not current
                and not jobs
                and local["controller_state"] == "observed"
            ):
                self.database.set_issue_state(
                    number,
                    "planning",
                    {"reason": "automatic GitHub issue intake"},
                )
                local = self.database.get_issue(number) or local

            if project_ready:
                self._sync_project(number)

            active = bool(current or jobs) or local["controller_state"] not in {
                "observed",
                "closed",
                "completed",
            }
            if enrolled or active:
                candidates.append(local)

        self._record(
            "github.issues_refreshed",
            count=len(candidates),
            backlog_count=len(open_issues),
        )
        return candidates

    def run_once(self) -> dict[str, Any]:
        started = utcnow()
        summary: dict[str, Any] = {
            "started_at": started,
            "apply": self.config.apply,
            "issues": [],
            "errors": [],
        }
        self._record("reconcile.started", apply=self.config.apply)
        try:
            issues = self.observe_issues()
        except Exception as exc:
            self._record("reconcile.authority_failed", authority="github", error=str(exc))
            raise
        for issue in issues:
            number = int(issue["number"])
            try:
                result = self.process_issue(number)
            except Exception as exc:
                error = {"issue": number, "error": str(exc), "type": type(exc).__name__}
                summary["errors"].append(error)
                self.database.event("issue.reconcile_failed", error, issue_number=number)
                self._record("issue.reconcile_failed", **error)
                continue
            summary["issues"].append(result)
        summary["finished_at"] = utcnow()
        self._record(
            "reconcile.finished",
            issue_count=len(summary["issues"]),
            error_count=len(summary["errors"]),
        )
        return summary

    def process_issue(self, issue_number: int) -> dict[str, Any]:
        live = self.github.issue(issue_number)
        issue = self.database.upsert_issue(live)
        if issue["github_state"] == "CLOSED":
            self._close_issue(issue_number)
            self._sync_project(issue_number)
            return self._issue_summary(issue_number)
        if not self.config.apply:
            return self._issue_summary(issue_number)

        current = self.database.current_plan(issue_number)
        jobs = self.database.list_jobs(issue_number)
        if current and current["plan"].get("issue_body_hash") != issue["body_hash"]:
            active = [job for job in jobs if job["state"] not in TERMINAL_JOB_STATES]
            if active:
                self.database.set_issue_state(
                    issue_number,
                    "blocked",
                    {"reason": "issue body changed after execution began"},
                )
                self.notifier.send(
                    f"issue:{issue_number}:body-drift:{issue['body_hash'][:12]}",
                    f"Alfred #{issue_number} is blocked: the issue changed after agents started. Re-plan or restore the approved scope in GitHub.",
                    {"issue": issue_number, "url": issue.get("url")},
                )
                self._sync_project(issue_number)
                return self._issue_summary(issue_number)
            self.database.invalidate_plan(issue_number, "issue body changed")
            current = None

        if current and not jobs and not self.database.is_approved(current["plan_hash"]):
            if current["status"] == "rejected":
                self._sync_project(issue_number)
                return self._issue_summary(issue_number)
            feedback = self.github.find_feedback(issue_number, after=current["created_at"])
            if feedback:
                self.database.invalidate_plan(
                    issue_number,
                    f"operator feedback in comment {feedback['comment_id']}",
                )
                self.notifier.send(
                    f"issue:{issue_number}:feedback:{feedback['comment_id']}",
                    f"Alfred #{issue_number} received specification feedback from @{feedback['actor']} and is replanning.",
                    {"issue": issue_number, "comment": feedback.get("url")},
                )
                self._sync_project(issue_number)
                current = None

        if current and not jobs and not self.database.is_approved(current["plan_hash"]):
            live_sha = self.github.default_branch_sha()
            if live_sha != current["base_sha"]:
                self.database.invalidate_plan(issue_number, "default branch advanced before approval")
                current = None

        if current is None:
            self.database.set_issue_state(issue_number, "planning")
            try:
                plan, plan_hash = self.planner.plan_issue(issue_number)
            except (AlfredCodeError, OSError) as exc:
                self.database.set_issue_state(issue_number, "blocked", {"reason": str(exc)})
                self.notifier.send(
                    f"issue:{issue_number}:planning-failed:{type(exc).__name__}",
                    f"Alfred #{issue_number} could not be specified: {exc}",
                    {"issue": issue_number},
                )
                self._sync_project(issue_number)
                raise
            self.database.save_plan(issue_number, plan_hash, plan)
            plan_url = self.github.post_plan(issue_number, plan, plan_hash)
            self.notifier.send(
                f"issue:{issue_number}:plan:{plan_hash}",
                f"Alfred #{issue_number} is specified in {len(plan['jobs'])} lane job(s). Approve plan {plan_hash[:12]} in GitHub: {plan_url or issue.get('url')}",
                {"issue": issue_number, "plan_hash": plan_hash, "url": plan_url},
            )
            current = self.database.current_plan(issue_number)
            if current is None:
                raise RuntimeError("saved plan disappeared")

        plan = current["plan"]
        plan_hash = current["plan_hash"]
        self.planner.revalidate(plan, plan_hash)
        self.github.post_plan(issue_number, plan, plan_hash)
        decision = self.github.find_decision(issue_number, plan_hash)
        if not self.database.is_approved(plan_hash):
            if decision is None:
                self._sync_project(issue_number)
                return self._issue_summary(issue_number)
            if decision["decision"] == "reject":
                self.database.reject_plan(
                    issue_number,
                    plan_hash,
                    decision["actor"],
                    decision["comment_id"],
                    decision.get("url"),
                    decision["created_at"],
                )
                self.notifier.send(
                    f"issue:{issue_number}:rejected:{plan_hash}",
                    f"Alfred #{issue_number} plan {plan_hash[:12]} was rejected by @{decision['actor']}.",
                    {"issue": issue_number, "plan_hash": plan_hash, "url": decision.get("url")},
                )
                self._sync_project(issue_number)
                return self._issue_summary(issue_number)
            live_sha = self.github.default_branch_sha()
            if live_sha != plan["base_sha"]:
                self.database.invalidate_plan(issue_number, "base advanced before approval was observed")
                self.notifier.send(
                    f"issue:{issue_number}:stale-approval:{plan_hash}",
                    f"Alfred #{issue_number}'s approval was rejected because main advanced. A fresh plan will be generated.",
                    {"issue": issue_number, "plan_hash": plan_hash},
                )
                self._sync_project(issue_number)
                return self._issue_summary(issue_number)
            self.database.record_approval(
                issue_number,
                plan_hash,
                decision["actor"],
                decision["comment_id"],
                decision.get("url"),
                decision["created_at"],
            )
            self.notifier.send(
                f"issue:{issue_number}:approved:{plan_hash}",
                f"Alfred #{issue_number} plan {plan_hash[:12]} was approved by @{decision['actor']} and is queued.",
                {"issue": issue_number, "plan_hash": plan_hash},
            )

        jobs = self.database.list_jobs(issue_number)
        if not jobs:
            jobs = self.database.materialize_jobs(issue_number, plan_hash, plan)
        for job in jobs:
            self.reconcile_job(issue, plan, job)
        self._derive_issue_state(issue_number)
        self._sync_project(issue_number)
        return self._issue_summary(issue_number)

    def _close_issue(self, issue_number: int) -> None:
        for job in self.database.list_jobs(issue_number):
            pr = self.github.pr_for_branch(job["branch"])
            if pr:
                self.database.observe("github", f"pr:{pr.number}", asdict(pr))
            if pr and pr.merged:
                self.database.update_job(
                    job["job_id"],
                    state="merged",
                    pr_number=pr.number,
                    pr_url=pr.url,
                    head_sha=pr.head_sha,
                )
            elif pr and not pr.closed_unmerged:
                self.database.update_job(
                    job["job_id"],
                    state="quarantined",
                    pr_number=pr.number,
                    pr_url=pr.url,
                    head_sha=pr.head_sha,
                    last_error="issue closed while PR remains open",
                )
            else:
                self.database.update_job(
                    job["job_id"],
                    state="closed",
                    last_error="GitHub issue or PR closed without merge",
                )
            self.database.release_lane(job["job_id"])
        self.database.set_issue_state(issue_number, "closed")

    def reconcile_job(self, issue: dict[str, Any], plan: dict[str, Any], job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        pr = self.github.pr_for_branch(job["branch"])
        if pr:
            self.database.observe("github", f"pr:{pr.number}", asdict(pr))
            if not job.get("workspace_id"):
                workspace = self.superset.workspace_by_name(
                    self._worker_workspace_name(int(issue["number"]), job["lane"])
                )
                if workspace and workspace.branch == job["branch"]:
                    job = self.database.update_job(
                        job_id,
                        workspace_id=workspace.id,
                        workspace_url=workspace.url,
                    )
            job = self.database.update_job(
                job_id,
                pr_number=pr.number,
                pr_url=pr.url,
                head_sha=pr.head_sha,
            )
            self._reconcile_pr(issue, plan, job, pr)
            return
        if job["state"] in TERMINAL_JOB_STATES:
            return
        if job["workspace_id"]:
            if (
                job["state"] == "blocked"
                and str(job.get("last_error") or "").startswith("worker made no repository progress")
            ):
                return
            details = self.superset.workspace_details(job["workspace_id"])
            self.database.observe("superset", f"workspace:{job['workspace_id']}", details)
            scope_error = self._worker_workspace_error(issue, plan, job, details)
            if scope_error:
                self._quarantine_worker(issue, plan, job, scope_error)
                return
            if self._finalize_worker_result(issue, plan, job, details):
                return
            status_blob = json.dumps(details).lower()
            if any(token in status_blob for token in ('"failed"', '"crashed"', '"terminated"')):
                self.database.update_job(job_id, state="blocked", last_error="Superset reports a failed agent session")
            elif self._worker_progress_timed_out(job, plan, details):
                timeout = self.config.superset.worker_progress_timeout_seconds
                error = (
                    f"worker made no repository progress within {timeout} seconds; "
                    "inspect the Superset terminal for an interactive startup prompt or failed agent"
                )
                self.database.update_job(job_id, state="blocked", last_error=error)
                self.database.release_lane(job_id)
                self.notifier.send(
                    f"job:{job_id}:no-progress:{plan['base_sha']}",
                    f"Alfred #{issue['number']} lane {job['lane']} is blocked: {error}.",
                    {"job": job_id, "workspace": job["workspace_id"]},
                )
            else:
                self.database.update_job(job_id, state="running")
            return
        dependencies = [self.database.get_job(dependency) for dependency in job["depends_on"]]
        if any(dependency is None or dependency["state"] != "merged" for dependency in dependencies):
            self.database.update_job(job_id, state="waiting_dependency")
            return
        if not self.database.acquire_lane(job["lane"], job_id):
            self.database.update_job(job_id, state="waiting_lane")
            return
        self.database.update_job(job_id, state="launching", last_error=None)
        try:
            self._prepare_branch(job["branch"], plan["base_sha"])
            workspace_name = self._worker_workspace_name(int(issue["number"]), job["lane"])
            existing = self.superset.workspace_by_name(workspace_name)
            if existing:
                if existing.branch != job["branch"]:
                    raise AuthorityUnavailable(
                        f"workspace {workspace_name} uses {existing.branch}, expected {job['branch']}"
                    )
                workspace, agent_id = existing, None
            else:
                workspace, agent_id = self.superset.create_worker(
                    repo_path=self.config.repo_path,
                    issue_number=int(issue["number"]),
                    job=job,
                    prompt=self.worker_prompt(issue, plan, job),
                )
            self.database.update_job(
                job_id,
                state="running",
                workspace_id=workspace.id,
                workspace_url=workspace.url,
                agent_id=agent_id,
            )
            self.notifier.send(
                f"job:{job_id}:launched:{plan['base_sha']}",
                f"Alfred #{issue['number']} lane {job['lane']} launched in Superset: {workspace.url or workspace.name}",
                {"job": job_id, "workspace": workspace.id},
            )
        except Exception as exc:
            self.database.update_job(job_id, state="blocked", last_error=str(exc))
            self.database.release_lane(job_id)
            raise

    def _reconcile_pr(
        self,
        issue: dict[str, Any],
        plan: dict[str, Any],
        job: dict[str, Any],
        pr: PullRequestObservation,
    ) -> None:
        job_id = job["job_id"]
        if pr.merged:
            self.database.update_job(job_id, state="merged", review_sha=pr.head_sha, last_error=None)
            self.database.release_lane(job_id)
            if (
                self.config.apply
                and self.config.superset.cleanup_merged_workspaces
                and job.get("workspace_id")
            ):
                self.superset.delete_workspace(job["workspace_id"])
            self.notifier.send(
                f"job:{job_id}:merged:{pr.head_sha}",
                f"Alfred #{issue['number']} lane {job['lane']} merged via PR #{pr.number}.",
                {"job": job_id, "pr": pr.url},
            )
            return
        if pr.closed_unmerged:
            self.database.update_job(job_id, state="quarantined", last_error="PR closed without merge")
            self.database.release_lane(job_id)
            self.notifier.send(
                f"job:{job_id}:closed:{pr.head_sha}",
                f"Alfred #{issue['number']} lane {job['lane']} was quarantined because PR #{pr.number} closed without merge.",
                {"job": job_id, "pr": pr.url},
            )
            return
        files = self.github.pr_files(pr.number)
        self.database.observe("github", f"pr:{pr.number}:files", files)
        outside = [
            path for path in files if not any(path_matches(path, allowed) for allowed in job["paths"])
        ]
        if outside:
            self.database.update_job(
                job_id,
                state="blocked",
                last_error=f"PR changes files outside its approved plan: {', '.join(outside)}",
            )
            self.notifier.send(
                f"job:{job_id}:scope-violation:{pr.head_sha}",
                f"Alfred #{issue['number']} PR #{pr.number} is blocked for out-of-lane files: {', '.join(outside)}",
                {"job": job_id, "pr": pr.url, "files": outside},
            )
            return
        if pr.ci == "RED":
            self.database.update_job(job_id, state="blocked", last_error="GitHub CI is red")
            self.notifier.send(
                f"job:{job_id}:ci-red:{pr.head_sha}",
                f"Alfred #{issue['number']} PR #{pr.number} has red CI and is blocked: {pr.url}",
                {"job": job_id, "pr": pr.url},
            )
            return
        if pr.ci != "GREEN":
            self.database.update_job(job_id, state="pr_open", last_error=None)
            return
        if "## Smoke evidence" not in pr.body:
            self.database.update_job(
                job_id,
                state="blocked",
                last_error="PR body has no ## Smoke evidence section",
            )
            return
        verdict = self.github.review_verdict(
            pr.number,
            pr.head_sha,
            not_before=(
                job.get("review_requested_at")
                if job.get("review_sha") == pr.head_sha
                else None
            ),
        ) if job.get("review_sha") == pr.head_sha else None
        if verdict is None and job.get("review_sha") == pr.head_sha and job.get("review_workspace_id"):
            if self._publish_review_result(issue, job, pr):
                return
            verdict = self.github.review_verdict(
                pr.number,
                pr.head_sha,
                not_before=job.get("review_requested_at"),
            )
        if verdict == "pass":
            if pr.is_draft:
                self.database.update_job(job_id, state="pr_open", last_error="PR is still a draft")
            elif pr.mergeable == "CONFLICTING":
                self.database.update_job(job_id, state="blocked", last_error="PR conflicts with its base")
            else:
                self.database.update_job(job_id, state="ready_merge", review_sha=pr.head_sha, last_error=None)
                self.notifier.send(
                    f"job:{job_id}:ready:{pr.head_sha}",
                    f"Alfred #{issue['number']} PR #{pr.number} is CI-green and independently reviewed at {pr.head_sha[:12]}. It is ready for your merge decision: {pr.url}",
                    {"job": job_id, "pr": pr.url, "sha": pr.head_sha},
                )
            return
        if verdict == "fail":
            self.database.update_job(job_id, state="blocked", review_sha=pr.head_sha, last_error="automated review failed")
            self.notifier.send(
                f"job:{job_id}:review-failed:{pr.head_sha}",
                f"Alfred #{issue['number']} PR #{pr.number} failed independent review at {pr.head_sha[:12]}: {pr.url}",
                {"job": job_id, "pr": pr.url, "sha": pr.head_sha},
            )
            return
        if job.get("review_sha") == pr.head_sha and job.get("review_requested_at"):
            self.database.update_job(job_id, state="reviewing")
            return
        review_name = self._review_workspace_name(pr.number, pr.head_sha)
        existing = self.superset.workspace_by_name(review_name)
        if existing:
            self.database.update_job(
                job_id,
                state="reviewing",
                review_sha=pr.head_sha,
                review_workspace_id=existing.id,
            )
            return
        self.database.update_job(
            job_id,
            state="reviewing",
            review_sha=pr.head_sha,
            review_requested_at=utcnow(),
        )
        review_branch = f"review/{pr.number}-{pr.head_sha[:12]}"
        self._prepare_exact_branch(review_branch, pr.head_sha)
        project_id = self.superset.ensure_project(self.config.repo_path)
        workspace, agent_id = self.superset.create_review_workspace(
            project_id,
            pr.number,
            review_name,
            review_branch,
            self.reviewer_prompt(issue, plan, job, pr),
            issue_number=int(issue["number"]),
            controller_job=job["job_id"],
            verify_command=job["verify_command"],
        )
        self.database.update_job(
            job_id,
            state="reviewing",
            review_workspace_id=workspace.id,
            review_agent_id=agent_id,
        )

    def _prepare_branch(self, branch: str, base_sha: str) -> None:
        repo = self.config.repo_path
        run(["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=repo)
        try:
            existing = run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo).strip()
        except CommandError:
            run(["git", "update-ref", f"refs/heads/{branch}", base_sha, ""], cwd=repo)
            return
        try:
            run(["git", "merge-base", "--is-ancestor", base_sha, existing], cwd=repo)
        except CommandError as exc:
            raise AuthorityUnavailable(
                f"existing branch {branch} is not descended from approved base {base_sha[:12]}"
            ) from exc

    def _prepare_exact_branch(self, branch: str, head_sha: str) -> None:
        repo = self.config.repo_path
        run(["git", "cat-file", "-e", f"{head_sha}^{{commit}}"], cwd=repo)
        try:
            existing = run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo).strip()
        except CommandError:
            run(["git", "update-ref", f"refs/heads/{branch}", head_sha, ""], cwd=repo)
            return
        if existing != head_sha:
            raise AuthorityUnavailable(
                f"existing review branch {branch} is {existing[:12]}, expected {head_sha[:12]}"
            )

    def _worker_progress_timed_out(
        self,
        job: dict[str, Any],
        plan: dict[str, Any],
        workspace: dict[str, Any],
    ) -> bool:
        path_value = workspace.get("worktreePath") or workspace.get("worktree_path")
        if not path_value:
            return False
        worktree = Path(str(path_value)).expanduser()
        if not worktree.is_dir():
            return False
        try:
            head = run(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=30).strip()
            status = run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=worktree,
                timeout=30,
            )
        except (CommandError, OSError):
            return False
        meaningful = [
            line
            for line in status.splitlines()
            if line.strip() and line[3:].strip() != ".lane"
        ]
        if head != str(plan["base_sha"]) or meaningful:
            return False
        try:
            started = datetime.fromisoformat(str(job["created_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started).total_seconds()
        return age >= self.config.superset.worker_progress_timeout_seconds

    @staticmethod
    def _workspace_path(workspace: dict[str, Any]) -> Path | None:
        value = workspace.get("worktreePath") or workspace.get("worktree_path")
        if not value:
            return None
        path = Path(str(value)).expanduser().resolve()
        return path if path.is_dir() else None

    @staticmethod
    def _status_paths(output: str) -> list[str]:
        paths: list[str] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            value = line[3:]
            if " -> " in value:
                old, new = value.split(" -> ", 1)
                paths.extend([old, new])
            else:
                paths.append(value)
        return paths

    def _changed_workspace_paths(self, worktree: Path, base_sha: str) -> list[str]:
        status = run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=worktree,
            timeout=30,
        )
        committed = run(
            ["git", "diff", "--name-only", f"{base_sha}...HEAD"],
            cwd=worktree,
            timeout=30,
        )
        return list(
            dict.fromkeys(
                [
                    path
                    for path in [*self._status_paths(status), *committed.splitlines()]
                    if path and path not in {".lane", WORKER_RESULT, REVIEW_RESULT}
                ]
            )
        )

    def _worker_workspace_error(
        self,
        issue: dict[str, Any],
        plan: dict[str, Any],
        job: dict[str, Any],
        workspace: dict[str, Any],
    ) -> str | None:
        worktree = self._workspace_path(workspace)
        if worktree is None:
            return None
        lane_path = worktree / ".lane"
        try:
            lane = json.loads(lane_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return f"enforced .lane manifest is missing or invalid: {exc}"
        expected = {
            "lane": job["lane"],
            "issue": int(issue["number"]),
            "allowed": job["paths"],
            "verify": job["verify_command"],
            "controller_job": job["job_id"],
            "role": "worker",
            "security_policy": SECURITY_POLICY,
        }
        if lane != expected:
            return "enforced .lane manifest drifted from the approved controller job"
        try:
            head = run(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=30).strip()
            if head != str(plan["base_sha"]):
                return "agent modified Git metadata; only the trusted controller may commit"
            changed = self._changed_workspace_paths(worktree, str(plan["base_sha"]))
        except (CommandError, OSError) as exc:
            return f"cannot prove workspace scope: {exc}"
        outside = [
            path for path in changed if not any(path_matches(path, allowed) for allowed in job["paths"])
        ]
        if outside:
            return f"workspace contains changes outside its approved plan: {', '.join(outside)}"
        try:
            deleted = run(
                ["git", "diff", "--name-only", "--diff-filter=D", str(plan["base_sha"])],
                cwd=worktree,
                timeout=30,
            ).splitlines()
        except (CommandError, OSError) as exc:
            return f"cannot prove non-destructive workspace state: {exc}"
        if deleted:
            return f"destructive file deletion is prohibited: {', '.join(deleted)}"
        return None

    def _quarantine_worker(
        self,
        issue: dict[str, Any],
        plan: dict[str, Any],
        job: dict[str, Any],
        error: str,
    ) -> None:
        self.database.update_job(job["job_id"], state="quarantined", last_error=error)
        self.database.release_lane(job["job_id"])
        self.notifier.send(
            f"job:{job['job_id']}:security:{plan['base_sha']}",
            f"Alfred #{issue['number']} lane {job['lane']} was quarantined by the scoped-agent policy: {error}.",
            {"job": job["job_id"], "workspace": job.get("workspace_id"), "error": error},
        )

    @staticmethod
    def _load_result(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"status": "invalid"}
        return value if isinstance(value, dict) else {"status": "invalid"}

    def _finalize_worker_result(
        self,
        issue: dict[str, Any],
        plan: dict[str, Any],
        job: dict[str, Any],
        workspace: dict[str, Any],
    ) -> bool:
        worktree = self._workspace_path(workspace)
        if worktree is None:
            return False
        result = self._load_result(worktree / WORKER_RESULT)
        if result is None:
            return False
        status = str(result.get("status") or "")
        if status == "blocked":
            reason = str(result.get("reason") or "worker reported an unspecified blocker")[:1000]
            self.database.update_job(job["job_id"], state="blocked", last_error=reason)
            self.database.release_lane(job["job_id"])
            return True
        if status != "ready":
            self._quarantine_worker(issue, plan, job, "worker result marker is invalid")
            return True
        changed = self._changed_workspace_paths(worktree, str(plan["base_sha"]))
        if not changed:
            self._quarantine_worker(issue, plan, job, "worker claimed readiness without repository changes")
            return True
        try:
            verification = run(
                ["bash", "-lc", job["verify_command"]],
                cwd=worktree,
                timeout=1200,
            )
            scope_error = self._worker_workspace_error(issue, plan, job, workspace)
            if scope_error:
                self._quarantine_worker(issue, plan, job, scope_error)
                return True
            changed = self._changed_workspace_paths(worktree, str(plan["base_sha"]))
            run(["git", "add", "--", *changed], cwd=worktree, timeout=60)
            staged = run(["git", "diff", "--cached", "--name-only"], cwd=worktree, timeout=30).splitlines()
            if not staged or set(staged) != set(changed):
                raise AuthorityUnavailable("staged diff does not exactly match the approved changed-file set")
            commit_message = f"feat: {job['title']} (#{issue['number']}, lane {job['lane']})"
            run(["git", "commit", "-m", commit_message], cwd=worktree, timeout=1200)
            run(["git", "push", "-u", "origin", job["branch"]], cwd=worktree, timeout=300)
            evidence = verification.strip() or "(command exited 0 with no output)"
            body = (
                f"Closes #{issue['number']}\n\n"
                f"Controller job: `{job['job_id']}` · lane `{job['lane']}`\n\n"
                "## Smoke evidence\n\n"
                f"`{job['verify_command']}`\n\n```text\n{evidence[:12000]}\n```\n\n"
                "The trusted controller independently reran this command after validating every changed path against the approved plan."
            )
            url = self.github.create_pr(
                branch=job["branch"],
                title=f"{issue['title']} (lane {job['lane']})",
                body=body,
            )
        except (AuthorityUnavailable, CommandError, OSError) as exc:
            self.database.update_job(job["job_id"], state="blocked", last_error=f"controller finalization failed: {exc}")
            self.database.release_lane(job["job_id"])
            self.notifier.send(
                f"job:{job['job_id']}:finalize-failed:{plan['base_sha']}",
                f"Alfred #{issue['number']} lane {job['lane']} passed agent handoff but trusted finalization failed: {exc}",
                {"job": job["job_id"], "workspace": job.get("workspace_id")},
            )
            return True
        self.database.update_job(job["job_id"], state="pr_open", pr_url=url, last_error=None)
        self.notifier.send(
            f"job:{job['job_id']}:pr-created:{job['branch']}",
            f"Alfred #{issue['number']} lane {job['lane']} was validated, committed, pushed, and opened as {url}.",
            {"job": job["job_id"], "pr": url},
        )
        return True

    def _publish_review_result(
        self,
        issue: dict[str, Any],
        job: dict[str, Any],
        pr: PullRequestObservation,
    ) -> bool:
        workspace_id = str(job.get("review_workspace_id") or "")
        if not workspace_id:
            return False
        details = self.superset.workspace_details(workspace_id)
        worktree = self._workspace_path(details)
        if worktree is None:
            return False
        expected_lane = {
            "lane": "review",
            "issue": int(issue["number"]),
            "allowed": [],
            "verify": job["verify_command"],
            "controller_job": job["job_id"],
            "role": "reviewer",
            "security_policy": SECURITY_POLICY,
        }
        try:
            lane = json.loads((worktree / ".lane").read_text())
            head = run(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=30).strip()
            status = run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=worktree,
                timeout=30,
            )
            changed = self._status_paths(status)
        except (OSError, json.JSONDecodeError, CommandError) as exc:
            error = f"cannot prove reviewer workspace integrity: {exc}"
        else:
            unexpected = [path for path in changed if path not in {".lane", REVIEW_RESULT}]
            if lane != expected_lane:
                error = "reviewer .lane manifest drifted from the controller job"
            elif head != pr.head_sha:
                error = f"reviewer HEAD drifted from exact PR SHA {pr.head_sha}"
            elif unexpected:
                error = f"reviewer modified read-only workspace paths: {', '.join(unexpected)}"
            else:
                error = None
        if error:
            self.database.update_job(job["job_id"], state="quarantined", last_error=error)
            self.notifier.send(
                f"job:{job['job_id']}:review-security:{pr.head_sha}",
                f"Alfred #{issue['number']} review workspace was quarantined: {error}.",
                {"job": job["job_id"], "workspace": workspace_id, "error": error},
            )
            return True
        result = self._load_result(worktree / REVIEW_RESULT)
        if result is None:
            return False
        verdict = str(result.get("verdict") or "fail").lower()
        if verdict not in {"pass", "fail"} or str(result.get("head_sha") or "") != pr.head_sha:
            verdict = "fail"
        findings = str(result.get("findings") or "Reviewer returned no findings summary.")[:8000]
        try:
            verification = run(
                ["bash", "-lc", job["verify_command"]],
                cwd=worktree,
                timeout=1200,
            )
        except (CommandError, OSError) as exc:
            verdict = "fail"
            verification = str(exc)
        marker = f"<!-- alfred-code-review:{pr.head_sha}:{verdict} -->"
        body = (
            f"## Alfred Code independent review\n\n{findings}\n\n"
            f"Verification: `{job['verify_command']}`\n\n```text\n"
            f"{(verification.strip() or '(command exited 0 with no output)')[:12000]}\n```\n\n{marker}"
        )
        self.github.post_pr_comment(pr.number, body)
        return False

    def _worker_workspace_name(self, issue_number: int, lane: str) -> str:
        return f"{self.config.superset.workspace_prefix}-{issue_number}-{lane.lower()}"

    def _review_workspace_name(self, pr_number: int, head_sha: str) -> str:
        return f"{self.config.superset.workspace_prefix}-review-{pr_number}-{head_sha[:8]}"

    def worker_prompt(self, issue: dict[str, Any], plan: dict[str, Any], job: dict[str, Any]) -> str:
        acceptance = "\n".join(f"- {item}" for item in self._planned_job(plan, job["job_id"]).get("acceptance", []))
        contracts = job.get("contracts") or {}
        return f"""Implement only controller job {job['job_id']} for GitHub issue #{issue['number']} in {self.config.github.repo}.

You own lane {job['lane']} on branch {job['branch']}, pinned from {plan['base_sha']}. The controller wrote .lane and the repository's actual lane hook is authoritative. Read current source, tests, CI configuration, and runtime logs; documentation is intent to verify, not proof.

Allowed write scope: {json.dumps(job['paths'])}
Required verification: {job['verify_command']}
Contracts to read: {json.dumps(contracts.get('read', []))}
Contracts changed by the approved phase0 job only: {json.dumps(contracts.get('changed', []))}

Acceptance evidence:
{acceptance or '- Satisfy the issue within the bounded lane scope and prove it with real tests.'}

Build the complete production implementation. Add or update real tests. Run the required verification and any narrower relevant tests. Inspect git diff and prove every changed file is in .lane. Do not commit, push, or call GitHub: the trusted controller owns Git metadata and external delivery.

When the implementation is complete, write `{WORKER_RESULT}` as one JSON object with `status` set to `ready` and a short `summary`. Write it only after your final test run, then stop. If blocked, write the same file with `status` set to `blocked` and a precise `reason`, then stop. The controller distrusts this marker: it independently validates scope, rejects deletions, reruns the lane verification, commits only the approved files, pushes the exact branch, and opens the PR.

Never merge, close, delete, reset, force-push, change another lane, use browser/computer/MCP tools, read or print secrets, disable hooks, or claim a test you did not run. If the lane boundary or contract is wrong, stop and report the blocker instead of escaping the scope.
"""

    def reviewer_prompt(
        self,
        issue: dict[str, Any],
        plan: dict[str, Any],
        job: dict[str, Any],
        pr: PullRequestObservation,
    ) -> str:
        return f"""Independently review PR #{pr.number} for issue #{issue['number']} at exact head SHA {pr.head_sha}.

Do not modify code. Verify the live diff, lane scope {json.dumps(job['paths'])}, contracts, acceptance criteria, and actual CI. Run the lane verification command `{job['verify_command']}` plus any focused tests needed to detect regressions. Documentation and PR prose are claims, not evidence.

Do not modify code, Git metadata, or GitHub. When finished, write `{REVIEW_RESULT}` as one JSON object containing `head_sha` exactly `{pr.head_sha}`, `verdict` set to `pass` only if this exact SHA is production-ready (otherwise `fail`), and a concise `findings` string with evidence. Then stop. The trusted controller reruns the enforced verification and posts the review marker to GitHub itself.

Never merge, approve through GitHub's review API, close, delete, push, use external-control tools, or expose secrets. If HEAD changes while reviewing, return `fail` for this SHA and stop.
"""

    @staticmethod
    def _planned_job(plan: dict[str, Any], job_id: str) -> dict[str, Any]:
        return next(item for item in plan["jobs"] if item["id"] == job_id)

    def _derive_issue_state(self, issue_number: int) -> None:
        jobs = self.database.list_jobs(issue_number)
        states = {job["state"] for job in jobs}
        if jobs and states <= {"merged"}:
            self.database.set_issue_state(issue_number, "completed")
        elif states.intersection({"blocked", "quarantined", "closed"}):
            self.database.set_issue_state(issue_number, "blocked")
        elif jobs and states <= {"ready_merge", "merged"}:
            self.database.set_issue_state(issue_number, "ready_merge")
        elif jobs:
            self.database.set_issue_state(issue_number, "building")

    def _sync_project(self, issue_number: int) -> None:
        if not self.project or not self.config.github.project_number:
            return
        issue = self.database.get_issue(issue_number)
        current = self.database.current_plan(issue_number)
        jobs = self.database.list_jobs(issue_number)
        if not issue:
            return
        try:
            self.project.sync_issue(
                project_number=self.config.github.project_number,
                issue_url=issue.get("url") or "",
                controller_state=issue["controller_state"],
                plan_hash=current["plan_hash"] if current else "",
                risk=current["plan"].get("risk", "") if current else "",
                lanes=[job["lane"] for job in jobs] or ([j["lane"] for j in current["plan"]["jobs"]] if current else []),
                runtime=", ".join(
                    f"{job['lane']}:{job['state']}" for job in jobs
                ),
            )
        except AuthorityUnavailable as exc:
            self.database.event(
                "project.sync_failed",
                {"error": str(exc)},
                issue_number=issue_number,
            )
            self._record("project.sync_failed", issue=issue_number, error=str(exc))

    def _issue_summary(self, issue_number: int) -> dict[str, Any]:
        issue = self.database.get_issue(issue_number) or {}
        current = self.database.current_plan(issue_number)
        return {
            "number": issue_number,
            "state": issue.get("controller_state"),
            "github_state": issue.get("github_state"),
            "plan_hash": current["plan_hash"] if current else None,
            "jobs": [
                {
                    "id": job["job_id"],
                    "lane": job["lane"],
                    "state": job["state"],
                    "pr": job.get("pr_number"),
                    "workspace": job.get("workspace_id"),
                    "error": job.get("last_error"),
                }
                for job in self.database.list_jobs(issue_number)
            ],
        }
