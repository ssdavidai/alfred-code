from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import GitHubConfig, TRUSTED_GITHUB_OPERATOR
from .errors import AuthorityUnavailable, CommandError
from .util import content_hash, run, run_json, utcnow


PLAN_MARKER = "<!-- alfred-code-plan:{plan_hash} -->"
AUTO_REPLAN_MARKER = "<!-- alfred-code-auto-replan:{plan_hash}:{evidence_hash} -->"
REVIEW_RE = re.compile(r"<!-- alfred-code-review:([0-9a-f]{40,64}):(pass|fail) -->")


@dataclass(frozen=True)
class PullRequestObservation:
    number: int
    url: str
    state: str
    head_sha: str
    ci: str
    merge_state: str
    mergeable: str
    is_draft: bool
    branch: str
    body: str = ""
    merged_at: str = ""

    @property
    def merged(self) -> bool:
        return self.state == "MERGED"

    @property
    def closed_unmerged(self) -> bool:
        return self.state == "CLOSED"


class GitHubClient:
    def __init__(self, config: GitHubConfig, binary: str = "gh"):
        self.config = config
        self.binary = binary
        self._issues: dict[int, dict[str, Any]] = {}
        self._issue_comments: dict[int, list[dict[str, Any]]] = {}
        self._open_issues: list[dict[str, Any]] | None = None
        self._pull_requests: dict[str, PullRequestObservation | None] = {}
        self._pr_comments: dict[int, list[dict[str, Any]]] = {}
        self._default_branch_sha: str | None = None
        self._authenticated_login: str | None = None

    def begin_cycle(self) -> None:
        """Drop observation caches at the start of one reconciliation cycle."""
        self._issues.clear()
        self._issue_comments.clear()
        self._open_issues = None
        self._pull_requests.clear()
        self._pr_comments.clear()
        self._default_branch_sha = None
        self._authenticated_login = None

    def invalidate_pr(self, branch: str) -> None:
        """Forget a PR observation after this controller mutates its branch."""
        self._pull_requests.pop(branch, None)

    def _run(self, arguments: list[str], *, timeout: int = 120) -> str:
        try:
            return run([self.binary, *arguments], timeout=timeout)
        except CommandError as exc:
            raise AuthorityUnavailable(str(exc)) from exc

    def _json(self, arguments: list[str], *, timeout: int = 120) -> Any:
        try:
            return run_json([self.binary, *arguments], timeout=timeout)
        except CommandError as exc:
            raise AuthorityUnavailable(str(exc)) from exc

    def doctor(self) -> dict[str, Any]:
        auth = self._json(["auth", "status", "--json", "hosts"])
        login = self.assert_trusted_operator()
        repo = self._json(["repo", "view", self.config.repo, "--json", "nameWithOwner,defaultBranchRef,url"])
        return {"auth": auth, "login": login, "repo": repo, "observed_at": utcnow()}

    def assert_trusted_operator(self) -> str:
        if self._authenticated_login is None:
            self._authenticated_login = self._run(["api", "user", "--jq", ".login"]).strip()
        if self._authenticated_login.casefold() != TRUSTED_GITHUB_OPERATOR:
            raise AuthorityUnavailable(
                "GitHub authentication is not the trusted operator "
                f"{TRUSTED_GITHUB_OPERATOR!r}: observed {self._authenticated_login!r}"
            )
        return TRUSTED_GITHUB_OPERATOR

    @staticmethod
    def _comment_actor(comment: dict[str, Any]) -> str:
        return str((comment.get("user") or {}).get("login") or "").strip()

    @classmethod
    def _is_trusted_comment(cls, comment: dict[str, Any]) -> bool:
        return cls._comment_actor(comment).casefold() == TRUSTED_GITHUB_OPERATOR

    def issue(self, number: int) -> dict[str, Any]:
        cached = self._issues.get(number)
        if cached is not None:
            return cached
        value = self._json(["api", f"repos/{self.config.repo}/issues/{number}"])
        issue = self._normalize_issue(value)
        if not isinstance(issue, dict):
            raise AuthorityUnavailable(f"GitHub issue #{number} returned a non-object response")
        self._issues[number] = issue
        return issue

    @staticmethod
    def _normalize_issue(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AuthorityUnavailable("GitHub issue endpoint returned a non-object response")
        return {
            "id": value.get("node_id") or value.get("id"),
            "number": value.get("number"),
            "title": value.get("title"),
            "body": value.get("body"),
            "state": str(value.get("state") or "OPEN").upper(),
            "url": value.get("html_url") or value.get("url"),
            "labels": value.get("labels") or [],
            "updatedAt": value.get("updatedAt") or value.get("updated_at"),
        }

    def open_issues(self, *, limit: int = 500) -> list[dict[str, Any]]:
        if self._open_issues is not None:
            return self._open_issues[:limit]
        if limit <= 0:
            return []
        result = self._json(
            [
                "api",
                f"repos/{self.config.repo}/issues?state=open&per_page={min(limit, 100)}",
                "--paginate",
                "--slurp",
            ]
        )
        if not isinstance(result, list):
            raise AuthorityUnavailable("GitHub issues endpoint returned a non-array response")
        pages = result if result and all(isinstance(page, list) for page in result) else [result]
        issues: list[dict[str, Any]] = []
        for page in pages:
            for value in page:
                if not isinstance(value, dict) or value.get("pull_request"):
                    continue
                issue = self._normalize_issue(value)
                if issue.get("number") is None:
                    continue
                issues.append(issue)
                self._issues[int(issue["number"])] = issue
                if len(issues) >= limit:
                    break
            if len(issues) >= limit:
                break
        self._open_issues = issues
        return issues

    def intake_issues(self, *, limit: int = 100) -> list[dict[str, Any]]:
        issues = self.open_issues(limit=500)
        if not self.config.intake_label:
            return issues[:limit]
        return [
            issue
            for issue in issues
            if self.config.intake_label
            in {
                str(label.get("name") or "") if isinstance(label, dict) else str(label)
                for label in issue.get("labels") or []
            }
        ][:limit]

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        cached = self._issue_comments.get(number)
        if cached is not None:
            return cached
        result = self._json(
            ["api", f"repos/{self.config.repo}/issues/{number}/comments", "--paginate"]
        )
        if not isinstance(result, list):
            raise AuthorityUnavailable("GitHub comments endpoint returned a non-array response")
        self._issue_comments[number] = result
        return result

    def post_issue_comment(self, number: int, body: str) -> str:
        self.assert_trusted_operator()
        result = self._run(
            ["issue", "comment", str(number), "--repo", self.config.repo, "--body", body]
        ).strip()
        self._issue_comments.pop(number, None)
        return result

    def post_auto_replan(
        self,
        issue_number: int,
        plan_hash: str,
        blockers: list[dict[str, Any]],
        completed: list[dict[str, Any]],
    ) -> str | None:
        evidence_hash = content_hash({"blockers": blockers, "completed": completed})[:16]
        marker = AUTO_REPLAN_MARKER.format(
            plan_hash=plan_hash,
            evidence_hash=evidence_hash,
        )
        for comment in self.issue_comments(issue_number):
            if self._is_trusted_comment(comment) and marker in str(comment.get("body") or ""):
                return comment.get("html_url")
        lines = [
            marker,
            "## Alfred Code automatic re-plan evidence",
            "",
            f"The approved plan `{plan_hash[:12]}` cannot finish as specified. The controller recognized only bounded planning conflicts, so it will preserve merged work and request a replacement plan for the remaining work.",
            "",
            "| Job | Lane | Classification | Observed blocker |",
            "|---|---|---|---|",
        ]
        for blocker in blockers:
            reason = str(blocker.get("reason") or "").replace("\n", " ").replace("|", "\\|")
            lines.append(
                f"| `{blocker['job_id']}` | `{blocker['lane']}` | `{blocker['kind']}` | {reason[:1000]} |"
            )
        if completed:
            lines.extend(
                [
                    "",
                    "Already merged work remains authoritative and must not be repeated:",
                    "",
                    *[
                        f"- `{job['job_id']}` in lane `{job['lane']}`"
                        + (f" via PR #{job['pr_number']}" if job.get("pr_number") else "")
                        for job in completed
                    ],
                ]
            )
        lines.extend(
            [
                "",
                "Any open PR retired by this re-plan is closed as superseded, but its branch, commits, and Superset workspace are retained for audit and recovery.",
                "",
                "The replacement plan will have new job, branch, and plan identities. The previous approval is revoked; a new exact `/approve-plan <full-new-hash>` comment is required before any replacement agent can launch.",
            ]
        )
        return self.post_issue_comment(issue_number, "\n".join(lines))

    def close_issue(self, number: int) -> None:
        self._run(
            [
                "issue",
                "close",
                str(number),
                "--repo",
                self.config.repo,
                "--reason",
                "completed",
            ]
        )
        self._issues.pop(number, None)
        self._issue_comments.pop(number, None)

    def reopen_issue(self, number: int) -> None:
        self._run(["issue", "reopen", str(number), "--repo", self.config.repo])
        self._issues.pop(number, None)
        self._issue_comments.pop(number, None)

    def update_pr_body(self, number: int, body: str) -> None:
        self._run(
            ["pr", "edit", str(number), "--repo", self.config.repo, "--body", body]
        )
        self._pull_requests.clear()
        self._pr_comments.pop(number, None)

    def create_pr(self, *, branch: str, title: str, body: str, base: str = "main") -> str:
        url = self._run(
            [
                "pr",
                "create",
                "--repo",
                self.config.repo,
                "--head",
                branch,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ]
        ).strip()
        self.invalidate_pr(branch)
        return url

    def post_pr_comment(self, number: int, body: str) -> str:
        self.assert_trusted_operator()
        result = self._run(
            ["pr", "comment", str(number), "--repo", self.config.repo, "--body", body]
        ).strip()
        self._pr_comments.pop(number, None)
        self._issue_comments.pop(number, None)
        return result

    def close_pr(self, number: int, comment: str) -> None:
        self.assert_trusted_operator()
        self._run(
            [
                "pr",
                "close",
                str(number),
                "--repo",
                self.config.repo,
                "--comment",
                comment,
            ]
        )
        self._pull_requests.clear()
        self._pr_comments.pop(number, None)

    def ensure_label(self, name: str, color: str, description: str) -> None:
        try:
            self._run(
                [
                    "label",
                    "create",
                    name,
                    "--repo",
                    self.config.repo,
                    "--color",
                    color,
                    "--description",
                    description,
                    "--force",
                ]
            )
        except AuthorityUnavailable:
            raise

    @staticmethod
    def plan_markdown(
        plan: dict[str, Any],
        plan_hash: str,
        approval_command: str,
        rejection_command: str,
    ) -> str:
        lines = [
            PLAN_MARKER.format(plan_hash=plan_hash),
            f"## Alfred Code execution plan `{plan_hash[:12]}`",
            "",
            plan.get("summary") or "No summary supplied.",
            "",
            f"Pinned base: `{plan['base_sha']}` · Risk: **{plan.get('risk', 'medium')}** · Story points: **{plan.get('story_points', 'legacy/unestimated')}**",
            "",
            f"Estimate evidence: {plan.get('points_evidence') or 'This legacy plan predates estimation.'}",
            "",
            "Issue dependencies: " + (
                ", ".join(f"#{number}" for number in plan.get("issue_dependencies", []))
                or "none"
            ),
            "",
            "| Order | Lane | Agent job | Paths | Verification |",
            "|---:|---|---|---|---|",
        ]
        for index, job in enumerate(plan["jobs"], start=1):
            dependencies = ", ".join(job.get("depends_on", [])) or "none"
            paths = "<br>".join(f"`{path}`" for path in job["paths"])
            verify = str(job["verify"]).replace("|", "\\|")
            lines.append(
                f"| {index} | `{job['lane']}` | **{job['id']}** — {job['title']}<br>after: {dependencies} | {paths} | `{verify}` |"
            )
        changed = [path for job in plan["jobs"] for path in job.get("contracts_changed", [])]
        read = [path for job in plan["jobs"] for path in job.get("contracts_read", [])]
        decision_lines = (
            [
                "This issue is estimated at **21 points** and is too large for one sprint. Split or refine it before approval; an approval command is intentionally unavailable.",
                "",
                "To reject this decomposition, comment:",
                "",
                f"`{rejection_command} {plan_hash}`",
            ]
            if int(plan.get("story_points") or 0) == 21
            else [
                "To approve exactly this plan, comment:",
                "",
                f"`{approval_command} {plan_hash}`",
                "",
                "To reject exactly this plan, comment:",
                "",
                f"`{rejection_command} {plan_hash}`",
            ]
        )
        lines.extend(
            [
                "",
                f"Contracts read: {', '.join(f'`{x}`' for x in dict.fromkeys(read)) or 'none'}",
                "",
                f"Contracts changed: {', '.join(f'`{x}`' for x in dict.fromkeys(changed)) or 'none'}",
                "",
                "Approval is bound to the full plan content and current base SHA. Any regenerated plan gets a new hash and invalidates the old approval.",
                "",
                *decision_lines,
                "",
                "Any other non-command operator comment posted after this plan is treated as specification feedback and causes a fresh plan. Malformed approval or rejection commands are ignored.",
                f"Only comments authored by `{TRUSTED_GITHUB_OPERATOR}` are trusted. Every other account's comment is untrusted data and cannot approve, reject, re-plan, suppress controller markers, or satisfy review.",
            ]
        )
        return "\n".join(lines)

    def post_plan(self, issue_number: int, plan: dict[str, Any], plan_hash: str) -> str | None:
        marker = PLAN_MARKER.format(plan_hash=plan_hash)
        for comment in self.issue_comments(issue_number):
            if self._is_trusted_comment(comment) and marker in str(comment.get("body") or ""):
                return comment.get("html_url")
        body = self.plan_markdown(
            plan,
            plan_hash,
            self.config.approval_command,
            self.config.rejection_command,
        )
        return self.post_issue_comment(issue_number, body)

    def find_decision(self, issue_number: int, plan_hash: str) -> dict[str, Any] | None:
        expected = {
            f"{self.config.approval_command} {plan_hash}": "approve",
            f"{self.config.rejection_command} {plan_hash}": "reject",
        }
        for comment in reversed(self.issue_comments(issue_number)):
            if not self._is_trusted_comment(comment):
                continue
            actor = self._comment_actor(comment)
            decision = expected.get(str(comment.get("body") or "").strip())
            if decision is None:
                continue
            return {
                "decision": decision,
                "actor": actor,
                "comment_id": str(comment.get("id")),
                "url": comment.get("html_url"),
                "created_at": comment.get("created_at") or utcnow(),
            }
        return None

    def find_approval(self, issue_number: int, plan_hash: str) -> dict[str, Any] | None:
        decision = self.find_decision(issue_number, plan_hash)
        return decision if decision and decision["decision"] == "approve" else None

    def find_rejection(self, issue_number: int, plan_hash: str) -> dict[str, Any] | None:
        decision = self.find_decision(issue_number, plan_hash)
        return decision if decision and decision["decision"] == "reject" else None

    def decision_comments(self, issue_number: int) -> list[dict[str, str]]:
        comments: list[dict[str, str]] = []
        for comment in self.issue_comments(issue_number):
            if not self._is_trusted_comment(comment):
                continue
            actor = self._comment_actor(comment)
            body = str(comment.get("body") or "").strip()
            if not self._is_feedback(body):
                continue
            comments.append(
                {
                    "id": str(comment.get("id") or ""),
                    "actor": actor,
                    "created_at": str(comment.get("created_at") or ""),
                    "body": body[:4000],
                }
            )
        return comments[-20:]

    def find_feedback(self, issue_number: int, *, after: str) -> dict[str, Any] | None:
        for comment in reversed(self.issue_comments(issue_number)):
            if not self._is_trusted_comment(comment):
                continue
            actor = self._comment_actor(comment)
            body = str(comment.get("body") or "").strip()
            created_at = str(comment.get("created_at") or "")
            if not created_at or created_at <= after:
                continue
            if not self._is_feedback(body):
                continue
            return {
                "actor": actor,
                "comment_id": str(comment.get("id") or ""),
                "url": comment.get("html_url"),
                "created_at": created_at,
                "body": body,
            }
        return None

    def _is_feedback(self, body: str) -> bool:
        if not body:
            return False
        if PLAN_MARKER.split("{")[0] in body or "<!-- alfred-code:spec" in body:
            return False
        if body.startswith("## Alfred Code spec"):
            return False
        control_commands = (
            self.config.approval_command,
            self.config.rejection_command,
        )
        for command in control_commands:
            if body == command:
                return False
            suffix = body[len(command) :] if body.startswith(command) else ""
            if suffix and suffix[0].isspace():
                return False
        return True

    def default_branch_sha(self) -> str:
        if self._default_branch_sha is not None:
            return self._default_branch_sha
        repo = self._json(["api", f"repos/{self.config.repo}"])
        branch = (
            str(repo.get("default_branch") or "main")
            if isinstance(repo, dict)
            else "main"
        )
        self._default_branch_sha = self._run(
            ["api", f"repos/{self.config.repo}/commits/{branch}", "--jq", ".sha"]
        ).strip()
        return self._default_branch_sha

    def refresh_default_branch_sha(self) -> str:
        self._default_branch_sha = None
        return self.default_branch_sha()

    @staticmethod
    def _ci_state(checks: list[dict[str, Any]]) -> str:
        if not checks:
            return "PENDING"
        values = {
            str(check.get("conclusion") or check.get("state") or check.get("status") or "PENDING").upper()
            for check in checks
        }
        if values.intersection({"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}):
            return "RED"
        if values.intersection({"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING"}):
            return "PENDING"
        if values.issubset({"SUCCESS", "NEUTRAL", "SKIPPED"}):
            return "GREEN"
        return "PENDING"

    def pr_for_branch(self, branch: str) -> PullRequestObservation | None:
        if branch in self._pull_requests:
            return self._pull_requests[branch]
        result = self._json(
            [
                "pr",
                "list",
                "--repo",
                self.config.repo,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "1",
                "--json",
                "number,url,state,headRefOid,headRefName,mergeStateStatus,mergeable,statusCheckRollup,isDraft,body,mergedAt",
            ]
        )
        if not result:
            self._pull_requests[branch] = None
            return None
        pr = result[0]
        observation = PullRequestObservation(
            number=int(pr["number"]),
            url=str(pr["url"]),
            state=str(pr["state"]).upper(),
            head_sha=str(pr.get("headRefOid") or ""),
            ci=self._ci_state(pr.get("statusCheckRollup") or []),
            merge_state=str(pr.get("mergeStateStatus") or "UNKNOWN").upper(),
            mergeable=str(pr.get("mergeable") or "UNKNOWN").upper(),
            is_draft=bool(pr.get("isDraft")),
            branch=str(pr.get("headRefName") or branch),
            body=str(pr.get("body") or ""),
            merged_at=str(pr.get("mergedAt") or ""),
        )
        self._pull_requests[branch] = observation
        return observation

    def pr_files(self, number: int) -> list[str]:
        result = self._json(
            ["api", f"repos/{self.config.repo}/pulls/{number}/files", "--paginate"]
        )
        if not isinstance(result, list):
            raise AuthorityUnavailable("GitHub PR files endpoint returned a non-array response")
        paths = [str(item.get("filename") or "") for item in result if item.get("filename")]
        if not paths:
            raise AuthorityUnavailable(f"GitHub returned no changed files for PR #{number}")
        return paths

    def pr_comments(self, number: int) -> list[dict[str, Any]]:
        cached = self._pr_comments.get(number)
        if cached is not None:
            return cached
        result = self._json(["api", f"repos/{self.config.repo}/issues/{number}/comments", "--paginate"])
        comments = result if isinstance(result, list) else []
        self._pr_comments[number] = comments
        return comments

    def review_verdict(
        self,
        pr_number: int,
        head_sha: str,
        *,
        not_before: str | None = None,
    ) -> str | None:
        result = self.review_feedback(
            pr_number,
            head_sha,
            not_before=not_before,
        )
        return str(result["verdict"]) if result else None

    def review_feedback(
        self,
        pr_number: int,
        head_sha: str,
        *,
        not_before: str | None = None,
    ) -> dict[str, str] | None:
        for comment in reversed(self.pr_comments(pr_number)):
            if not self._is_trusted_comment(comment):
                continue
            actor = self._comment_actor(comment)
            created_at = str(comment.get("created_at") or "")
            if not_before and created_at < not_before:
                continue
            body = str(comment.get("body") or "")
            for sha, verdict in REVIEW_RE.findall(body):
                if sha == head_sha:
                    return {
                        "verdict": verdict,
                        "body": body[:12000],
                        "url": str(comment.get("html_url") or ""),
                        "created_at": created_at,
                        "actor": actor,
                    }
        return None

    def open_prs(self) -> list[dict[str, Any]]:
        result = self._json(
            [
                "pr",
                "list",
                "--repo",
                self.config.repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,headRefName,url,body",
            ]
        )
        return result if isinstance(result, list) else []
