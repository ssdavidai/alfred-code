from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from alfred_code import dashboard
from alfred_code.config import ControllerConfig
from alfred_code.config import GitHubConfig
from alfred_code.dashboard import DashboardData, TelemetryScanner, serve_dashboard
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
