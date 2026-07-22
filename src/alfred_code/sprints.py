from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import ControllerConfig
from .db import Database
from .github import GitHubClient
from .project import ProjectBoard


class SprintManager:
    """Explicit operator controls for the sprint commitment boundary."""

    def __init__(
        self,
        config: ControllerConfig,
        database: Database,
        project: ProjectBoard,
        github: GitHubClient | None = None,
    ):
        if not config.github.project_number:
            raise ValueError("github.project_number is required for sprint management")
        self.config = config
        self.database = database
        self.project = project
        self.github = github or GitHubClient(config.github)
        self.project_number = int(config.github.project_number)

    def start(
        self,
        *,
        title: str | None = None,
        duration_days: int | None = None,
    ) -> dict[str, Any]:
        if self.database.active_sprint():
            active = self.database.active_sprint() or {}
            raise RuntimeError(f"Sprint {active.get('number')} is already active")
        duration = int(duration_days or self.config.sprint_duration_days)
        if not 1 <= duration <= 42:
            raise ValueError("sprint duration must be between 1 and 42 days")
        self.project.refresh(self.project_number, force=True)
        queued = [
            item
            for item in self.project.delivery_items(self.project_number)
            if item["stage"] == "Sprint queue"
        ]
        if not queued:
            raise ValueError(
                "Sprint queue is empty; drag at least one Inbox card into Sprint queue first"
            )
        issue_numbers = [int(item["issue_number"]) for item in queued]
        for issue_number in issue_numbers:
            if self.database.get_issue(issue_number) is None:
                self.database.upsert_issue(self.github.issue(issue_number))
        number = self.database.next_sprint_number()
        sprint_title = title or (
            "Sprint 0 — Calibration" if number == 0 else f"Sprint {number}"
        )
        start = datetime.now(timezone.utc).replace(microsecond=0)
        end = start + timedelta(days=duration)
        starts_at = start.isoformat().replace("+00:00", "Z")
        ends_at = end.isoformat().replace("+00:00", "Z")
        iteration_id = self.project.ensure_sprint_iteration(
            self.project_number,
            title=sprint_title,
            starts_at=starts_at,
            duration_days=duration,
        )
        sprint = self.database.start_sprint(
            title=sprint_title,
            duration_days=duration,
            starts_at=starts_at,
            ends_at=ends_at,
            iteration_id=iteration_id,
            issue_numbers=issue_numbers,
        )
        return {**sprint, "issues": issue_numbers}
