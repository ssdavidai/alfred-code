from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import GitHubConfig
from .errors import AuthorityUnavailable, CommandError
from .util import run, run_json, utcnow


PLAN_MARKER = "<!-- alfred-code-plan:{plan_hash} -->"
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
        repo = self._json(["repo", "view", self.config.repo, "--json", "nameWithOwner,defaultBranchRef,url"])
        return {"auth": auth, "repo": repo, "observed_at": utcnow()}

    def issue(self, number: int) -> dict[str, Any]:
        return self._json(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                self.config.repo,
                "--json",
                "id,number,title,body,state,url,labels,updatedAt",
            ]
        )

    def open_issues(self, *, limit: int = 500) -> list[dict[str, Any]]:
        result = self._json(
            [
                "issue",
                "list",
                "--repo",
                self.config.repo,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "id,number,title,body,state,url,labels,updatedAt",
            ]
        )
        if not isinstance(result, list):
            raise AuthorityUnavailable("GitHub issue list returned a non-array response")
        return result

    def intake_issues(self, *, limit: int = 100) -> list[dict[str, Any]]:
        arguments = [
            "issue",
            "list",
            "--repo",
            self.config.repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "id,number,title,body,state,url,labels,updatedAt",
        ]
        if self.config.intake_label:
            arguments.extend(["--label", self.config.intake_label])
        result = self._json(arguments)
        if not isinstance(result, list):
            raise AuthorityUnavailable("GitHub issue list returned a non-array response")
        return result

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        result = self._json(
            ["api", f"repos/{self.config.repo}/issues/{number}/comments", "--paginate"]
        )
        if not isinstance(result, list):
            raise AuthorityUnavailable("GitHub comments endpoint returned a non-array response")
        return result

    def post_issue_comment(self, number: int, body: str) -> str:
        return self._run(
            ["issue", "comment", str(number), "--repo", self.config.repo, "--body", body]
        ).strip()

    def create_pr(self, *, branch: str, title: str, body: str, base: str = "main") -> str:
        return self._run(
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

    def post_pr_comment(self, number: int, body: str) -> str:
        return self._run(
            ["pr", "comment", str(number), "--repo", self.config.repo, "--body", body]
        ).strip()

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
            f"Pinned base: `{plan['base_sha']}` · Risk: **{plan.get('risk', 'medium')}**",
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
        lines.extend(
            [
                "",
                f"Contracts read: {', '.join(f'`{x}`' for x in dict.fromkeys(read)) or 'none'}",
                "",
                f"Contracts changed: {', '.join(f'`{x}`' for x in dict.fromkeys(changed)) or 'none'}",
                "",
                "Approval is bound to the full plan content and current base SHA. Any regenerated plan gets a new hash and invalidates the old approval.",
                "",
                "To approve exactly this plan, comment:",
                "",
                f"`{approval_command} {plan_hash}`",
                "",
                "To reject exactly this plan, comment:",
                "",
                f"`{rejection_command} {plan_hash}`",
                "",
                "Any other non-command operator comment posted after this plan is treated as specification feedback and causes a fresh plan. Malformed approval or rejection commands are ignored.",
            ]
        )
        return "\n".join(lines)

    def post_plan(self, issue_number: int, plan: dict[str, Any], plan_hash: str) -> str | None:
        marker = PLAN_MARKER.format(plan_hash=plan_hash)
        for comment in self.issue_comments(issue_number):
            if marker in str(comment.get("body") or ""):
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
        approvers = {actor.lower() for actor in self.config.approvers}
        for comment in reversed(self.issue_comments(issue_number)):
            actor = str((comment.get("user") or {}).get("login") or "")
            if actor.lower() not in approvers:
                continue
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
        approvers = {actor.lower() for actor in self.config.approvers}
        comments: list[dict[str, str]] = []
        for comment in self.issue_comments(issue_number):
            actor = str((comment.get("user") or {}).get("login") or "")
            body = str(comment.get("body") or "").strip()
            if actor.lower() not in approvers or not self._is_feedback(body):
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
        approvers = {actor.lower() for actor in self.config.approvers}
        for comment in reversed(self.issue_comments(issue_number)):
            actor = str((comment.get("user") or {}).get("login") or "")
            body = str(comment.get("body") or "").strip()
            created_at = str(comment.get("created_at") or "")
            if actor.lower() not in approvers or not created_at or created_at <= after:
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
        repo = self._json(
            ["repo", "view", self.config.repo, "--json", "defaultBranchRef"]
        )
        branch = ((repo.get("defaultBranchRef") or {}).get("name") if isinstance(repo, dict) else None) or "main"
        return self._run(["api", f"repos/{self.config.repo}/commits/{branch}", "--jq", ".sha"]).strip()

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
                "number,url,state,headRefOid,headRefName,mergeStateStatus,mergeable,statusCheckRollup,isDraft,body",
            ]
        )
        if not result:
            return None
        pr = result[0]
        return PullRequestObservation(
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
        )

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
        result = self._json(["api", f"repos/{self.config.repo}/issues/{number}/comments", "--paginate"])
        return result if isinstance(result, list) else []

    def review_verdict(
        self,
        pr_number: int,
        head_sha: str,
        *,
        not_before: str | None = None,
    ) -> str | None:
        reviewers = {actor.lower() for actor in self.config.reviewers}
        for comment in reversed(self.pr_comments(pr_number)):
            actor = str((comment.get("user") or {}).get("login") or "").lower()
            if actor not in reviewers:
                continue
            if not_before and str(comment.get("created_at") or "") < not_before:
                continue
            for sha, verdict in REVIEW_RE.findall(str(comment.get("body") or "")):
                if sha == head_sha:
                    return verdict
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
