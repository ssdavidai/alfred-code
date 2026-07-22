from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from alfred_code.config import ControllerConfig, GitHubConfig
from alfred_code.db import Database
from alfred_code.sprints import SprintManager


class FakeProject:
    def __init__(self, items):
        self.items = items
        self.refreshes = []
        self.iterations = []

    def refresh(self, number, *, force=False):
        self.refreshes.append((number, force))

    def delivery_items(self, number):
        return list(self.items)

    def ensure_sprint_iteration(self, number, *, title, starts_at, duration_days):
        self.iterations.append((number, title, starts_at, duration_days))
        return "iteration-0"


class NoGitHubCalls:
    def issue(self, number):
        raise AssertionError(f"unexpected GitHub lookup for #{number}")


def issue(number: int) -> dict:
    return {
        "id": f"I_{number}",
        "number": number,
        "title": f"Issue {number}",
        "body": "Build it",
        "state": "OPEN",
        "url": f"https://example/issues/{number}",
        "labels": [],
    }


def test_start_sprint_commits_github_queue_in_manual_order(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(repo="owner/repo", owner="owner", project_number=3),
    )
    database = Database(config.database_path)
    database.upsert_issue(issue(7))
    database.upsert_issue(issue(9))
    project = FakeProject(
        [
            {"issue_number": 9, "stage": "Sprint queue", "rank": 1},
            {"issue_number": 7, "stage": "Sprint queue", "rank": 4},
            {"issue_number": 8, "stage": "Inbox", "rank": 2},
        ]
    )

    sprint = SprintManager(config, database, project, NoGitHubCalls()).start()

    assert sprint["title"] == "Sprint 0 — Calibration"
    assert sprint["issues"] == [9, 7]
    assert sprint["iteration_id"] == "iteration-0"
    assert [item["issue_number"] for item in database.sprint_items(sprint["id"])] == [9, 7]
    database.close()


def test_start_sprint_refuses_empty_queue(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(repo="owner/repo", owner="owner", project_number=3),
    )
    database = Database(config.database_path)

    with pytest.raises(ValueError, match="Sprint queue is empty"):
        SprintManager(config, database, FakeProject([]), NoGitHubCalls()).start()

    database.close()
