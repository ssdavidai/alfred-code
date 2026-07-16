from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

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
        intake = {int(issue["number"]): issue for issue in self.github.intake_issues()}
        for tracked in self.database.list_issues():
            number = int(tracked["number"])
            if number not in intake:
                intake[number] = self.github.issue(number)
        observed = []
        for number in sorted(intake):
            local = self.database.upsert_issue(intake[number])
            observed.append(local)
            self.database.observe("github", f"issue:{number}", intake[number])
        self._record("github.issues_refreshed", count=len(observed))
        return observed

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
        approval = self.github.find_approval(issue_number, plan_hash)
        if not self.database.is_approved(plan_hash):
            if approval is None:
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
                approval["actor"],
                approval["comment_id"],
                approval.get("url"),
                approval["created_at"],
            )
            self.notifier.send(
                f"issue:{issue_number}:approved:{plan_hash}",
                f"Alfred #{issue_number} plan {plan_hash[:12]} was approved by @{approval['actor']} and is queued.",
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
            details = self.superset.workspace_details(job["workspace_id"])
            self.database.observe("superset", f"workspace:{job['workspace_id']}", details)
            status_blob = json.dumps(details).lower()
            if any(token in status_blob for token in ('"failed"', '"crashed"', '"terminated"')):
                self.database.update_job(job_id, state="blocked", last_error="Superset reports a failed agent session")
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
        project_id = self.superset.ensure_project(self.config.repo_path)
        workspace, agent_id = self.superset.create_review_workspace(
            project_id,
            pr.number,
            review_name,
            self.reviewer_prompt(issue, plan, job, pr),
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

Build the complete production implementation. Add or update real tests. Run the required verification and any narrower relevant tests. Before pushing, inspect git diff and prove every changed file is in .lane. Commit intentionally, push {job['branch']}, and open one PR that references #{issue['number']} and includes exact commands plus their actual outputs under a `## Smoke evidence` heading.

Never merge, close, delete, reset, force-push, change another lane, read or print secrets, disable hooks, or claim a test you did not run. If the lane boundary or contract is wrong, stop and explain the blocker in the workspace instead of escaping the scope.
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

Post exactly one GitHub PR comment. Include findings and command evidence, then finish with exactly one marker:

<!-- alfred-code-review:{pr.head_sha}:pass -->

only if the exact SHA is production-ready, or:

<!-- alfred-code-review:{pr.head_sha}:fail -->

if any actionable defect, scope violation, missing test, or unverifiable behavior remains. Never merge, approve through GitHub's review API, close, delete, push, or expose secrets. If HEAD changes while reviewing, post fail for this SHA and stop.
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
