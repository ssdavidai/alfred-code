from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from alfred_code.config import ControllerConfig, GitHubConfig
from alfred_code.db import Database
from alfred_code.splits import IssueSplitter


def _issue(number: int) -> dict:
    return {
        "id": f"I_{number}",
        "number": number,
        "title": f"Issue {number}",
        "body": "Build it",
        "state": "OPEN",
        "url": f"https://example.test/issues/{number}",
        "labels": [],
    }


def _plan() -> dict:
    return {
        "schema": 1,
        "issue": 42,
        "base_sha": "a" * 40,
        "issue_body_hash": "body",
        "summary": "A cross-lane feature that is too large for one delivery.",
        "risk": "high",
        "story_points": 21,
        "points_evidence": "Two implementation lanes and a contract migration exceed one bounded delivery.",
        "issue_dependencies": [],
        "jobs": [
            {
                "id": "contract-42",
                "lane": "phase0",
                "title": "Freeze the contract",
                "branch": "phase0/42-contract",
                "paths": ["contracts/feature.md"],
                "verify": "pytest tests/contracts",
                "contracts_read": ["contracts/current.md"],
                "contracts_changed": ["contracts/feature.md"],
                "depends_on": [],
                "acceptance": ["Contract tests pass."],
            },
            {
                "id": "api-42",
                "lane": "I",
                "title": "Implement the API",
                "branch": "lane-1/42-api",
                "paths": ["api/**"],
                "verify": "pytest tests/api",
                "contracts_read": ["contracts/feature.md"],
                "contracts_changed": [],
                "depends_on": ["contract-42"],
                "acceptance": ["API tests pass."],
            },
        ],
    }


class FakeGitHub:
    def __init__(self, *, fail_summary_once: bool = False):
        self.issues: dict[int, dict] = {}
        self.created = 0
        self.links: list[tuple[int, int]] = []
        self.summaries = 0
        self.fail_summary_once = fail_summary_once

    def issues_by_markers(self, markers):
        return {
            marker: issue
            for marker in markers
            for issue in self.issues.values()
            if marker in str(issue.get("body") or "")
        }

    def create_issue(self, *, title, body):
        self.created += 1
        number = 1000 + self.created
        issue = {
            **_issue(number),
            "id": f"I_{number}",
            "title": title,
            "body": body,
        }
        self.issues[number] = issue
        return issue

    def issue(self, number):
        return self.issues[number]

    def add_sub_issue(self, parent, child):
        if (parent, child) not in self.links:
            self.links.append((parent, child))

    def post_split_summary(self, issue_number, plan_hash, children):
        if self.fail_summary_once:
            self.fail_summary_once = False
            raise RuntimeError("temporary comment failure")
        self.summaries += 1
        return f"https://example.test/issues/{issue_number}#split"


class FakeProject:
    def __init__(self, *, fail_once: bool = False):
        self.fail_once = fail_once
        self.synced: list[dict] = []

    def sync_issue(self, **values):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary project failure")
        self.synced.append(values)


def _setup(tmp_path: Path):
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(repo="owner/repo", owner="owner", project_number=3),
    )
    database = Database(config.database_path)
    database.upsert_issue(_issue(42))
    plan = _plan()
    plan_hash = "f" * 64
    database.save_plan(42, plan_hash, plan)
    assert database.mark_plan_needs_split(42, plan_hash)
    return config, database, plan_hash


def test_operator_split_creates_native_children_in_inbox_and_is_idempotent(
    tmp_path: Path,
) -> None:
    config, database, plan_hash = _setup(tmp_path)
    github = FakeGitHub()
    project = FakeProject()
    splitter = IssueSplitter(config, database, project, github)  # type: ignore[arg-type]

    result = splitter.split(42)

    assert result["status"] == "completed"
    assert [child["status"] for child in result["children"]] == ["ready", "ready"]
    assert github.created == 2
    assert github.links == [(42, 1001), (42, 1002)]
    assert github.summaries == 1
    assert len(project.synced) == 2
    assert all(item["product_stage"] == "inbox" for item in project.synced)
    assert database.get_issue(42)["product_stage"] == "needs_split"
    assert database.current_plan(42)["status"] == "needs_split"
    assert database.get_issue(1001)["product_stage"] == "inbox"
    assert plan_hash in github.issues[1001]["body"]
    assert "not approved work" in github.issues[1001]["body"]

    repeated = splitter.split(42)

    assert repeated["status"] == "completed"
    assert github.created == 2
    assert github.summaries == 1
    database.close()


def test_failed_split_resumes_without_duplicate_children(tmp_path: Path) -> None:
    config, database, _plan_hash = _setup(tmp_path)
    github = FakeGitHub()
    project = FakeProject(fail_once=True)
    splitter = IssueSplitter(config, database, project, github)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="temporary project failure"):
        splitter.split(42)

    failed = database.issue_split(42)
    assert failed["status"] == "failed"
    assert failed["children"][0]["child_issue_number"] == 1001
    assert failed["children"][0]["linked_at"] is not None

    completed = splitter.split(42)

    assert completed["status"] == "completed"
    assert github.created == 2
    assert github.links == [(42, 1001), (42, 1002)]
    assert [child["child_issue_number"] for child in completed["children"]] == [1001, 1002]
    database.close()


def test_split_refuses_a_plan_that_is_not_in_needs_splitting(tmp_path: Path) -> None:
    config, database, _plan_hash = _setup(tmp_path)
    database.connection.execute(
        "UPDATE issues SET product_stage='inbox' WHERE number=42"
    )

    with pytest.raises(RuntimeError, match="Needs splitting"):
        IssueSplitter(config, database, FakeProject(), FakeGitHub()).split(42)  # type: ignore[arg-type]

    database.close()


def test_summary_failure_resumes_without_replaying_ready_children(tmp_path: Path) -> None:
    config, database, _plan_hash = _setup(tmp_path)
    github = FakeGitHub(fail_summary_once=True)
    project = FakeProject()
    splitter = IssueSplitter(config, database, project, github)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="temporary comment failure"):
        splitter.split(42)
    assert database.issue_split(42)["status"] == "failed"
    assert github.created == 2
    assert len(project.synced) == 2

    completed = splitter.split(42)

    assert completed["status"] == "completed"
    assert github.created == 2
    assert len(project.synced) == 2
    assert github.summaries == 1
    database.close()
