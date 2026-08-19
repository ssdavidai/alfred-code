from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from urllib import error, request

import pytest

from alfred_code import dashboard
from alfred_code.config import ControllerConfig
from alfred_code.config import GitHubConfig
from alfred_code.dashboard import (
    DashboardData,
    DashboardHTTPServer,
    TelemetryScanner,
    serve_dashboard,
)
from alfred_code.db import Database


def test_snapshot_maps_durable_state_without_writing(tmp_path: Path) -> None:
    config = replace(ControllerConfig(), state_dir=tmp_path)
    database = Database(config.database_path)
    database.upsert_issue(
        {
            "number": 42,
            "title": "Make the controller observable",
            "body": "Show live truth.",
            "state": "OPEN",
            "url": "https://example.test/issues/42",
            "labels": [{"name": "observability"}],
        }
    )
    database.set_issue_state(42, "planning")
    database.close()
    before = config.database_path.read_bytes()

    data = DashboardData(config)
    data.telemetry.sessions = lambda: []  # type: ignore[method-assign]
    snapshot = data.snapshot()

    assert snapshot["runtime"]["read_only"] is True
    assert snapshot["issues"][0]["number"] == 42
    assert snapshot["issues"][0]["column"] == "specifying"
    assert next(column for column in snapshot["columns"] if column["id"] == "specifying") == {
        "id": "specifying",
        "label": "Specifying",
        "count": 1,
    }
    assert "Planner instrumentation is active" in snapshot["analytics"]["telemetry_note"]
    assert config.database_path.read_bytes() == before


def test_closed_github_issue_cannot_render_in_an_active_column(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(project_number=3),
    )
    database = Database(config.database_path)
    database.upsert_issue(
        {
            "number": 42,
            "title": "Historically stale closure",
            "body": "No longer active.",
            "state": "CLOSED",
            "url": "https://example.test/issues/42",
            "labels": [],
        }
    )
    database.set_issue_state(42, "awaiting_approval")
    database.close()

    data = DashboardData(config)
    data.telemetry.sessions = lambda: []  # type: ignore[method-assign]
    snapshot = data.snapshot()

    assert snapshot["issues"][0]["column"] == "done"
    assert next(column for column in snapshot["columns"] if column["id"] == "backlog")["count"] == 0
    assert next(column for column in snapshot["columns"] if column["id"] == "done")["count"] == 1


def test_needs_split_card_exposes_reason_proposed_children_and_action(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(project_number=3),
    )
    database = Database(config.database_path)
    database.upsert_issue(
        {
            "number": 42,
            "title": "Large feature",
            "body": "Build several bounded parts.",
            "state": "OPEN",
            "url": "https://example.test/issues/42",
            "labels": [],
        }
    )
    plan = {
        "schema": 1,
        "issue": 42,
        "base_sha": "a" * 40,
        "summary": "Split the contract from implementation.",
        "risk": "high",
        "story_points": 21,
        "points_evidence": "Cross-lane contract and implementation work exceeds one delivery.",
        "issue_dependencies": [],
        "jobs": [
            {
                "id": "contract-42",
                "lane": "phase0",
                "title": "Freeze contract",
                "paths": ["contracts/feature.md"],
                "verify": "pytest tests/contracts",
                "contracts_read": [],
                "contracts_changed": ["contracts/feature.md"],
                "depends_on": [],
                "acceptance": ["Contract passes."],
            }
        ],
    }
    digest = "f" * 64
    database.save_plan(42, digest, plan)
    database.mark_plan_needs_split(42, digest)
    database.close()

    data = DashboardData(config)
    data.telemetry.sessions = lambda: []  # type: ignore[method-assign]
    snapshot = data.snapshot()
    card = snapshot["issues"][0]

    assert card["column"] == "needs_split"
    assert card["plan"]["points_evidence"].startswith("Cross-lane")
    assert card["plan"]["proposed_jobs"] == [
        {
            "id": "contract-42",
            "lane": "phase0",
            "title": "Freeze contract",
            "paths": ["contracts/feature.md"],
            "verify": "pytest tests/contracts",
            "contracts_read": [],
            "contracts_changed": ["contracts/feature.md"],
            "depends_on": [],
            "acceptance": ["Contract passes."],
        }
    ]
    assert card["split"] is None
    assert snapshot["runtime"]["controlled_actions"] == [
        "start_sprint",
        "split_issue",
        "approve_plan",
    ]


def test_split_http_action_requires_token_and_dispatches_explicit_click(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(project_number=3),
    )
    database = Database(config.database_path)
    database.close()
    data = DashboardData(config)
    calls = []
    data.split_issue = lambda number: calls.append(number) or {  # type: ignore[method-assign]
        "status": "completed"
    }
    server = DashboardHTTPServer(("127.0.0.1", 0), data, b"dashboard")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/issues/42/split"
    try:
        denied = request.Request(
            url,
            data=json.dumps({"token": "wrong"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(error.HTTPError) as raised:
            request.urlopen(denied, timeout=2)
        assert raised.value.code == 403
        assert calls == []

        allowed = request.Request(
            url,
            data=json.dumps({"token": server.action_token}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(allowed, timeout=2) as response:
            payload = json.loads(response.read())
        assert payload["split"]["status"] == "completed"
        assert calls == [42]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_approve_plan_posts_trusted_comment_without_shortcutting_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(project_number=3),
    )
    database = Database(config.database_path)
    database.upsert_issue(
        {
            "number": 42,
            "title": "Bounded feature",
            "body": "Ship one lane.",
            "state": "OPEN",
            "url": "https://example.test/issues/42",
            "labels": [],
        }
    )
    plan = {
        "base_sha": "a" * 40,
        "summary": "Implement one bounded feature.",
        "risk": "low",
        "story_points": 8,
        "jobs": [],
    }
    digest = "b" * 64
    database.save_plan(42, digest, plan)
    database.close()

    calls = []

    class FakeGitHub:
        def __init__(self, _config: GitHubConfig):
            pass

        def post_plan_approval(self, number: int, plan_hash: str) -> dict:
            calls.append((number, plan_hash))
            return {
                "created": True,
                "actor": "ssdavidai",
                "comment_url": "https://example.test/comment",
                "plan_hash": plan_hash,
            }

    monkeypatch.setattr(dashboard, "GitHubClient", FakeGitHub)
    result = DashboardData(config).approve_plan(42, digest)

    assert result["created"] is True
    assert calls == [(42, digest)]
    database = Database(config.database_path)
    assert database.current_plan(42)["status"] == "awaiting_approval"
    assert database.get_issue(42)["controller_state"] == "awaiting_approval"
    assert database.is_approved(digest) is False
    database.close()

    with pytest.raises(ValueError, match="plan is stale"):
        DashboardData(config).approve_plan(42, "c" * 64)


def test_approve_plan_refuses_oversized_plan(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(project_number=3),
    )
    database = Database(config.database_path)
    database.upsert_issue(
        {
            "number": 42,
            "title": "Oversized feature",
            "body": "Split me.",
            "state": "OPEN",
            "url": "https://example.test/issues/42",
            "labels": [],
        }
    )
    digest = "d" * 64
    database.save_plan(
        42,
        digest,
        {
            "base_sha": "a" * 40,
            "summary": "Too much for one delivery.",
            "risk": "high",
            "story_points": 21,
            "jobs": [],
        },
    )
    database.close()

    with pytest.raises(ValueError, match="must be split"):
        DashboardData(config).approve_plan(42, digest)


def test_approve_http_action_requires_token_and_full_hash(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(project_number=3),
    )
    Database(config.database_path).close()
    data = DashboardData(config)
    calls = []
    data.approve_plan = lambda number, plan_hash: calls.append(  # type: ignore[method-assign]
        (number, plan_hash)
    ) or {"created": True, "plan_hash": plan_hash}
    server = DashboardHTTPServer(("127.0.0.1", 0), data, b"dashboard")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/issues/42/approve"
    digest = "e" * 64
    try:
        denied = request.Request(
            url,
            data=json.dumps({"token": "wrong", "plan_hash": digest}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(error.HTTPError) as raised:
            request.urlopen(denied, timeout=2)
        assert raised.value.code == 403
        assert calls == []

        allowed = request.Request(
            url,
            data=json.dumps(
                {"token": server.action_token, "plan_hash": digest}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(allowed, timeout=2) as response:
            payload = json.loads(response.read())
            assert response.status == 201
        assert payload["approval"]["plan_hash"] == digest
        assert calls == [(42, digest)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_html_keeps_split_creation_behind_operator_button() -> None:
    html = (
        Path(__file__).parents[1]
        / "src"
        / "alfred_code"
        / "static"
        / "dashboard.html"
    ).read_text()

    assert "Why this needs splitting" in html
    assert "Split into ${proposedJobs.length} child issue" in html
    assert "Nothing has been created yet" in html
    assert "splitIssue(number)" in html


def test_dashboard_html_presents_complete_plan_and_operator_approval() -> None:
    html = (
        Path(__file__).parents[1]
        / "src"
        / "alfred_code"
        / "static"
        / "dashboard.html"
    ).read_text()

    assert "Plan for approval" in html
    assert "Acceptance criteria" in html
    assert "Contracts changed" in html
    assert "Verification" in html
    assert "Approve ${issue.plan.story_points}-point plan" in html
    assert "approvePlan(number, issue.plan.hash)" in html
    assert "plan_hash: planHash" in html
    assert "exact full-hash command" in html


def test_codex_session_uses_last_cumulative_token_count(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    records = [
        {
            "timestamp": "2026-07-20T09:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": "/tmp/lane-2/317-work"},
        },
        {
            "timestamp": "2026-07-20T09:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-test"},
        },
        {
            "timestamp": "2026-07-20T09:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    }
                },
            },
        },
        {
            "timestamp": "2026-07-20T09:02:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 240,
                        "cached_input_tokens": 180,
                        "output_tokens": 44,
                        "reasoning_output_tokens": 9,
                        "total_tokens": 284,
                    }
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    scanner = TelemetryScanner(tmp_path / "controller.sqlite3")

    usage = scanner._parse_codex(
        path,
        "session-id",
        {"issue_number": 317, "job_id": "learn-317", "role": "worker", "status": "completed"},
    )

    assert usage is not None
    assert usage.model == "gpt-test"
    assert usage.output_tokens == 44
    assert usage.total_tokens == 284
    assert usage.issue_number == 317


def test_claude_session_deduplicates_streamed_message_records(tmp_path: Path) -> None:
    path = tmp_path / "claude.jsonl"
    usage = {
        "input_tokens": 3,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 20,
        "output_tokens": 7,
    }
    records = [
        {
            "type": "assistant",
            "timestamp": "2026-07-20T09:00:00Z",
            "cwd": "/tmp/lane-1/42-work",
            "message": {"id": "message-1", "model": "claude-test", "usage": usage},
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-20T09:00:01Z",
            "cwd": "/tmp/lane-1/42-work",
            "message": {"id": "message-1", "model": "claude-test", "usage": usage},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    scanner = TelemetryScanner(tmp_path / "controller.sqlite3")

    result = scanner._parse_claude(
        path,
        "session-id",
        {"issue_number": 42, "job_id": "job-42", "role": "worker", "status": "completed"},
    )

    assert result is not None
    assert result.model == "claude-test"
    assert result.output_tokens == 7
    assert result.total_tokens == 40


def test_planner_telemetry_is_attributed_to_issue_and_model(tmp_path: Path) -> None:
    database = Database(tmp_path / "control-plane.sqlite3")
    database.close()
    telemetry = tmp_path / "planner-telemetry.jsonl"
    telemetry.write_text(
        json.dumps(
            {
                "at": "2026-07-20T09:03:00Z",
                "kind": "planner.usage",
                "issue_number": 58,
                "provider": "claude",
                "session_id": "planner-session",
                "started_at": "2026-07-20T09:00:00Z",
                "duration_ms": 180000,
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                    "output_tokens": 30,
                },
                "model_usage": {
                    "claude-test": {
                        "inputTokens": 5,
                        "cacheReadInputTokens": 100,
                        "cacheCreationInputTokens": 20,
                        "outputTokens": 30,
                    }
                },
            }
        )
        + "\n"
    )
    scanner = TelemetryScanner(tmp_path / "control-plane.sqlite3")

    sessions = scanner._planner_sessions()

    assert len(sessions) == 1
    assert sessions[0].issue_number == 58
    assert sessions[0].model == "claude-test"
    assert sessions[0].role == "planner"
    assert sessions[0].total_tokens == 155


def test_codex_planner_telemetry_preserves_provider_and_cumulative_totals(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "control-plane.sqlite3")
    database.close()
    telemetry = tmp_path / "planner-telemetry.jsonl"
    telemetry.write_text(
        json.dumps(
            {
                "at": "2026-07-20T09:03:00Z",
                "kind": "planner.usage",
                "issue_number": 333,
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "session_id": "planner-session",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
                "model_usage": {
                    "gpt-5.6-sol": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                    }
                },
            }
        )
        + "\n"
    )
    scanner = TelemetryScanner(tmp_path / "control-plane.sqlite3")

    sessions = scanner._planner_sessions()

    assert len(sessions) == 1
    assert sessions[0].provider == "codex"
    assert sessions[0].model == "gpt-5.6-sol"
    assert sessions[0].cached_input_tokens == 80
    assert sessions[0].reasoning_tokens == 5
    assert sessions[0].total_tokens == 120


def test_dashboard_refuses_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        serve_dashboard(ControllerConfig(), host="0.0.0.0", port=0)


def test_sprint_velocity_uses_completed_points_and_time_bounded_tokens(tmp_path: Path) -> None:
    config = replace(
        ControllerConfig(),
        state_dir=tmp_path,
        github=GitHubConfig(project_number=3),
    )
    database = Database(config.database_path)
    database.upsert_issue(
        {
            "number": 42,
            "title": "Measured sprint item",
            "body": "Ship it",
            "state": "OPEN",
            "url": "https://example.test/issues/42",
            "labels": [],
        }
    )
    sprint = database.start_sprint(
        title="Sprint 0 — Calibration",
        duration_days=14,
        starts_at="2026-07-20T00:00:00Z",
        ends_at="2026-08-03T00:00:00Z",
        iteration_id="iteration-0",
        issue_numbers=[42],
    )
    database.record_story_points(42, 5, "bounded implementation")
    database.set_sprint_item_status(42, "done")
    database.close()
    data = DashboardData(config)
    data.telemetry.sessions = lambda: [  # type: ignore[method-assign]
        {
            "session_id": "one",
            "provider": "codex",
            "model": "gpt-test",
            "issue_number": 42,
            "job_id": "job-42",
            "role": "worker",
            "workspace": "test",
            "started_at": "2026-07-21T00:00:00Z",
            "ended_at": "2026-07-21T00:01:00Z",
            "status": "completed",
            "input_tokens": 70,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 30,
            "reasoning_tokens": 10,
            "total_tokens": 100,
        }
    ]

    snapshot = data.snapshot()
    measured = snapshot["analytics"]["active_sprint"]

    assert measured["id"] == sprint["id"]
    assert measured["completed_points"] == 5
    assert measured["tokens"]["total_tokens"] == 100
    assert measured["tokens_per_completed_point"] == 20


def test_runtime_reports_every_parallel_safe_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dashboard,
        "_read_processes",
        lambda: [
            {
                "pid": 100,
                "ppid": 1,
                "elapsed": "01:00",
                "command": "python -m alfred_code.cli serve",
            },
            {
                "pid": 101,
                "ppid": 100,
                "elapsed": "00:10",
                "command": 'codex exec --profile alfred-planner --model gpt-5.6-sol -c model_reasoning_effort="high" --output-schema /tmp/alfred-code-plan-325-a/plan.schema.json --json -',
            },
            {
                "pid": 102,
                "ppid": 100,
                "elapsed": "00:08",
                "command": 'codex exec --profile alfred-planner --model gpt-5.6-sol -c model_reasoning_effort="high" --output-schema /tmp/alfred-code-plan-326-b/plan.schema.json --json -',
            },
        ],
    )

    runtime = dashboard._controller_runtime()

    assert [planner["issue"] for planner in runtime["planners"]] == [325, 326]
    assert runtime["planner"]["issue"] == 325
    assert runtime["planner"]["provider"] == "codex"
    assert runtime["planner"]["model"] == "gpt-5.6-sol"
    assert runtime["planner"]["effort"] == "high"
    assert all(planner["safe_mode"] for planner in runtime["planners"])
