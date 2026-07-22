from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_security import SCOPED_AGENT_IDS, SECURITY_POLICY
from .config import SupersetConfig
from .errors import AuthorityUnavailable, CommandError
from .util import canonical_json, run_json


def worker_workspace_name(
    prefix: str,
    issue_number: int,
    lane: str,
    job_id: str,
) -> str:
    """Give every controller job a stable, human-readable workspace identity."""
    normalized = re.sub(r"[^a-z0-9]+", "-", job_id.lower()).strip("-") or "job"
    readable = normalized[:32].rstrip("-") or "job"
    digest = hashlib.sha256(job_id.encode()).hexdigest()[:8]
    return f"{prefix}-{issue_number}-{lane.lower()}-{readable}-{digest}"


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    branch: str
    url: str | None = None


class SupersetClient:
    def __init__(self, config: SupersetConfig):
        self.config = config

    def _command(self, arguments: list[str]) -> list[str]:
        command = [self.config.cli, *arguments, "--json"]
        api_key = os.environ.get(self.config.api_key_env, "").strip()
        if api_key:
            command.extend(["--api-key", api_key])
        return command

    def _json(self, arguments: list[str], timeout: int = 180) -> Any:
        command = self._command(arguments)
        try:
            return run_json(command, timeout=timeout)
        except CommandError as exc:
            raise AuthorityUnavailable(str(exc)) from exc

    @staticmethod
    def _assert_scoped_agent(agent: str) -> None:
        if agent not in SCOPED_AGENT_IDS:
            raise AuthorityUnavailable(
                f"refusing unsafe Superset agent {agent!r}; Alfred Code accepts only provisioned scoped agent UUIDs"
            )

    @staticmethod
    def _items(value: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = value.get(key) or value.get("data") or []
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return []

    @staticmethod
    def _id(value: dict[str, Any]) -> str:
        identifier = value.get("id") or value.get("workspaceId") or value.get("projectId")
        if not identifier:
            raise AuthorityUnavailable(f"Superset returned no id: {json.dumps(value)[:500]}")
        return str(identifier)

    def doctor(self) -> dict[str, Any]:
        return {
            "whoami": self._json(["auth", "whoami"]),
            "status": self._json(["status"]),
            "projects": self._json(["projects", "list"]),
        }

    def projects(self) -> list[dict[str, Any]]:
        return self._items(self._json(["projects", "list"]), "projects")

    def ensure_project(self, repo_path: Path) -> str:
        for project in self.projects():
            if str(project.get("name") or "").lower() == self.config.project_name.lower():
                return self._id(project)
        created = self._json(
            [
                "projects",
                "create",
                "--local",
                "--name",
                self.config.project_name,
                "--import",
                str(repo_path),
            ],
            timeout=300,
        )
        if not isinstance(created, dict):
            raise AuthorityUnavailable("Superset project creation returned a non-object response")
        return self._id(created)

    def workspaces(self, search: str | None = None) -> list[Workspace]:
        arguments = ["workspaces", "list", "--local", "--project", self.config.project_name]
        if search:
            arguments.extend(["--search", search])
        result = self._items(self._json(arguments), "workspaces")
        return [
            Workspace(
                id=self._id(item),
                name=str(item.get("name") or ""),
                branch=str(item.get("branch") or item.get("branchName") or ""),
                url=item.get("url") or item.get("deeplink") or item.get("webUrl"),
            )
            for item in result
        ]

    def workspace_for_branch(self, branch: str) -> Workspace | None:
        return next((workspace for workspace in self.workspaces(branch) if workspace.branch == branch), None)

    def workspace_by_name(self, name: str) -> Workspace | None:
        return next((workspace for workspace in self.workspaces(name) if workspace.name == name), None)

    def workspace_details(self, workspace_id: str) -> dict[str, Any]:
        result = self._json(["workspaces", "get", workspace_id])
        if not isinstance(result, dict):
            raise AuthorityUnavailable("Superset workspace lookup returned a non-object response")
        return result

    def create_worker(
        self,
        *,
        repo_path: Path,
        issue_number: int,
        job: dict[str, Any],
        prompt: str,
    ) -> tuple[Workspace, str | None]:
        self._assert_scoped_agent(self.config.worker_agent)
        name = worker_workspace_name(
            self.config.workspace_prefix,
            issue_number,
            str(job["lane"]),
            str(job["job_id"]),
        )
        existing = self.workspace_for_branch(job["branch"])
        if existing:
            if existing.name != name:
                raise AuthorityUnavailable(
                    f"branch {job['branch']} already belongs to Superset workspace {existing.name!r}, not {name!r}"
                )
            return existing, None
        project_id = self.ensure_project(repo_path)
        lane_document = {
            "lane": job["lane"],
            "issue": issue_number,
            "allowed": job["paths"],
            "verify": job["verify_command"],
            "controller_job": job["job_id"],
            "role": "worker",
            "security_policy": SECURITY_POLICY,
        }
        encoded = base64.b64encode((canonical_json(lane_document) + "\n").encode()).decode()
        command = f"printf '%s' {encoded} | base64 --decode > .lane"
        result = self._json(
            [
                "workspaces",
                "create",
                "--local",
                "--project",
                project_id,
                "--name",
                name,
                "--branch",
                job["branch"],
                "--base-branch",
                "main",
                "--command",
                command,
                "--agent",
                self.config.worker_agent,
                "--prompt",
                prompt,
            ],
            timeout=300,
        )
        if not isinstance(result, dict):
            raise AuthorityUnavailable("Superset workspace creation returned a non-object response")
        workspace_data = result.get("workspace") if isinstance(result.get("workspace"), dict) else result
        workspace = Workspace(
            id=self._id(workspace_data),
            name=str(workspace_data.get("name") or name),
            branch=str(workspace_data.get("branch") or workspace_data.get("branchName") or job["branch"]),
            url=workspace_data.get("url") or workspace_data.get("deeplink") or workspace_data.get("webUrl"),
        )
        agent_data = result.get("agent") if isinstance(result.get("agent"), dict) else {}
        agent_id = agent_data.get("id") or result.get("agentId")
        return workspace, str(agent_id) if agent_id else None

    def start_reviewer(self, workspace_id: str, prompt: str) -> str | None:
        return self.start_agent(workspace_id, self.config.reviewer_agent, prompt)

    def start_agent(self, workspace_id: str, agent: str, prompt: str) -> str | None:
        self._assert_scoped_agent(agent)
        result = self._json(
            [
                "agents",
                "create",
                "--workspace",
                workspace_id,
                "--agent",
                agent,
                "--prompt",
                prompt,
            ],
            timeout=300,
        )
        if isinstance(result, dict):
            identifier = result.get("id") or result.get("agentId") or (result.get("agent") or {}).get("id")
            return str(identifier) if identifier else None
        return None

    def create_review_workspace(
        self,
        project_id: str,
        pr_number: int,
        name: str,
        branch: str,
        prompt: str,
        *,
        issue_number: int,
        controller_job: str,
        verify_command: str,
    ) -> tuple[Workspace, str | None]:
        self._assert_scoped_agent(self.config.reviewer_agent)
        lane_document = {
            "lane": "review",
            "issue": issue_number,
            "allowed": [],
            "verify": verify_command,
            "controller_job": controller_job,
            "role": "reviewer",
            "security_policy": SECURITY_POLICY,
        }
        encoded = base64.b64encode((canonical_json(lane_document) + "\n").encode()).decode()
        command = f"printf '%s' {encoded} | base64 --decode > .lane"
        result = self._json(
            [
                "workspaces",
                "create",
                "--local",
                "--project",
                project_id,
                "--name",
                name,
                "--branch",
                branch,
                "--base-branch",
                "main",
                "--command",
                command,
                "--agent",
                self.config.reviewer_agent,
                "--prompt",
                prompt,
            ],
            timeout=300,
        )
        if not isinstance(result, dict):
            raise AuthorityUnavailable("Superset review workspace creation returned a non-object response")
        data = result.get("workspace") if isinstance(result.get("workspace"), dict) else result
        workspace = Workspace(
            id=self._id(data),
            name=str(data.get("name") or name),
            branch=str(data.get("branch") or data.get("branchName") or branch),
            url=data.get("url") or data.get("deeplink") or data.get("webUrl"),
        )
        agent_data = result.get("agent") if isinstance(result.get("agent"), dict) else {}
        agent_id = agent_data.get("id") or result.get("agentId")
        return workspace, str(agent_id) if agent_id else None

    def delete_workspace(self, workspace_id: str) -> None:
        self._json(["workspaces", "delete", workspace_id, "--local"], timeout=300)
