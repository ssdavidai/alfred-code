from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import ControllerConfig
from .db import Database
from .github import GitHubClient
from .project import ProjectBoard
from .splits import IssueSplitter
from .sprints import SprintManager


KANBAN_COLUMNS = [
    ("backlog", "Backlog"),
    ("inbox", "Inbox"),
    ("sprint_queue", "Sprint queue"),
    ("specifying", "Specifying"),
    ("approval", "Approval"),
    ("queued", "Queued"),
    ("building", "Building"),
    ("ready_merge", "Ready to merge"),
    ("blocked", "Blocked"),
    ("needs_split", "Needs splitting"),
    ("done", "Done"),
]

STATE_TO_COLUMN = {
    "observed": "inbox",
    "planning": "specifying",
    "awaiting_approval": "approval",
    "approved": "queued",
    "building": "building",
    "ready_merge": "ready_merge",
    "blocked": "blocked",
    "completed": "done",
    "closed": "done",
}

PRODUCT_TO_COLUMN = {
    "backlog": "backlog",
    "inbox": "inbox",
    "sprint_queue": "sprint_queue",
    "needs_split": "needs_split",
    "done": "done",
}

UUID_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
ISSUE_PATH_RE = re.compile(r"/(?:lane-[^/]+|phase0)/(\d+)-")
REVIEW_PATH_RE = re.compile(r"/review/(\d+)-")
PLANNER_ISSUE_RE = re.compile(r'\\?"const\\?"\s*:\s*(\d+)')
PLANNER_SCHEMA_ISSUE_RE = re.compile(r"alfred-code-plan-(\d+)-")
PLANNER_MODEL_RE = re.compile(r"(?:^|\s)--model\s+(\S+)")
PLANNER_EFFORT_RE = re.compile(r"(?:^|\s)--effort\s+(\S+)")
CODEX_EFFORT_RE = re.compile(r'model_reasoning_effort\s*=\s*\\?["\']?([a-z]+)', re.I)
SPLIT_ACTION_RE = re.compile(r"^/api/issues/([1-9][0-9]*)/split$")
APPROVE_ACTION_RE = re.compile(r"^/api/issues/([1-9][0-9]*)/approve$")
PLAN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _iso_to_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _duration_seconds(start: str | None, end: str | None = None) -> int | None:
    left = _iso_to_seconds(start)
    right = _iso_to_seconds(end) if end else time.time()
    if left is None or right is None:
        return None
    return max(0, int(right - left))


def _read_processes() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": ppid, "elapsed": parts[2], "command": parts[3]})
    return rows


def _controller_runtime() -> dict[str, Any]:
    rows = _read_processes()
    controller = next(
        (
            row
            for row in rows
            if "alfred_code.cli" in row["command"] and " serve" in row["command"]
        ),
        None,
    )
    planners: list[dict[str, Any]] = []
    if controller:
        children = [
            row
            for row in rows
            if row["ppid"] == controller["pid"]
            and (
                "--permission-mode plan" in row["command"]
                or (
                    re.search(r"(?:^|/)codex\s+exec(?:\s|$)", row["command"])
                    and "--profile alfred-planner" in row["command"]
                    and "--output-schema" in row["command"]
                )
            )
        ]
        for child in children:
            is_codex = bool(re.search(r"(?:^|/)codex\s+exec(?:\s|$)", child["command"]))
            match = PLANNER_ISSUE_RE.search(child["command"])
            if match is None:
                match = PLANNER_SCHEMA_ISSUE_RE.search(child["command"])
            model_match = PLANNER_MODEL_RE.search(child["command"])
            effort_match = (
                CODEX_EFFORT_RE.search(child["command"])
                if is_codex
                else PLANNER_EFFORT_RE.search(child["command"])
            )
            planners.append(
                {
                    "pid": child["pid"],
                    "elapsed": child["elapsed"],
                    "issue": int(match.group(1)) if match else None,
                    "provider": "codex" if is_codex else "claude",
                    "model": model_match.group(1) if model_match else "configured default",
                    "effort": effort_match.group(1) if effort_match else None,
                    "safe_mode": (
                        "--profile alfred-planner" in child["command"]
                        if is_codex
                        else "--permission-mode plan" in child["command"]
                    ),
                }
            )
        planners.sort(key=lambda item: (item["issue"] is None, item["issue"] or 0))
    return {
        "running": controller is not None,
        "pid": controller["pid"] if controller else None,
        "elapsed": controller["elapsed"] if controller else None,
        "planner": planners[0] if planners else None,
        "planners": planners,
    }


def _pid_is_running(path: Path) -> tuple[bool, int | None]:
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False, None
    return True, pid


@dataclass
class SessionUsage:
    session_id: str
    provider: str
    model: str
    issue_number: int | None
    job_id: str | None
    role: str
    workspace: str
    started_at: str | None
    ended_at: str | None
    status: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class TelemetryScanner:
    """Read persisted CLI telemetry and attribute it through Superset workspaces."""

    def __init__(self, controller_db: Path):
        self.controller_db = controller_db
        self.superset_host_db = self._find_superset_host_db()
        self._file_index: dict[str, Path] = {}
        self._index_at = 0.0
        self._cache: dict[Path, tuple[int, int, SessionUsage | None]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _find_superset_host_db() -> Path | None:
        candidates = sorted(Path("~/.superset/host").expanduser().glob("*/host.db"))
        return candidates[0] if candidates else None

    def _refresh_file_index(self) -> None:
        if time.time() - self._index_at < 10:
            return
        index: dict[str, Path] = {}
        roots = [Path("~/.codex/sessions").expanduser(), Path("~/.claude/projects").expanduser()]
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                match = UUID_RE.search(path.name)
                if match:
                    index[match.group("id")] = path
        self._file_index = index
        self._index_at = time.time()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection

    def _workspace_attribution(self) -> tuple[dict[str, dict[str, Any]], dict[int, int]]:
        workspace_map: dict[str, dict[str, Any]] = {}
        pr_to_issue: dict[int, int] = {}
        try:
            connection = self._connect(self.controller_db)
            rows = connection.execute(
                """
                SELECT issue_number, job_id, state, pr_number, workspace_id, review_workspace_id
                FROM jobs
                """
            ).fetchall()
            connection.close()
        except (OSError, sqlite3.Error):
            rows = []
        for row in rows:
            if row["workspace_id"]:
                workspace_map[row["workspace_id"]] = {
                    "issue_number": int(row["issue_number"]),
                    "job_id": row["job_id"],
                    "role": "worker",
                    "job_state": row["state"],
                }
            if row["review_workspace_id"]:
                workspace_map[row["review_workspace_id"]] = {
                    "issue_number": int(row["issue_number"]),
                    "job_id": row["job_id"],
                    "role": "reviewer",
                    "job_state": row["state"],
                }
            if row["pr_number"]:
                pr_to_issue[int(row["pr_number"])] = int(row["issue_number"])
        return workspace_map, pr_to_issue

    def _bindings(self) -> list[dict[str, Any]]:
        if not self.superset_host_db or not self.superset_host_db.exists():
            return []
        try:
            connection = self._connect(self.superset_host_db)
            rows = connection.execute(
                """
                SELECT b.agent_session_id, b.agent_id, b.started_at, b.last_event_at,
                       b.last_event_type, b.workspace_id, w.worktree_path, w.name
                FROM terminal_agent_bindings b
                JOIN workspaces w ON w.id = b.workspace_id
                WHERE b.agent_session_id IS NOT NULL
                ORDER BY b.started_at
                """
            ).fetchall()
            connection.close()
        except (OSError, sqlite3.Error):
            return []
        return [dict(row) for row in rows]

    @staticmethod
    def _infer_issue(path: str, pr_to_issue: dict[int, int]) -> int | None:
        match = ISSUE_PATH_RE.search(path)
        if match:
            return int(match.group(1))
        review = REVIEW_PATH_RE.search(path)
        if review:
            return pr_to_issue.get(int(review.group(1)))
        return None

    def _parse_codex(
        self, path: Path, session_id: str, base: dict[str, Any]
    ) -> SessionUsage | None:
        cwd = str(base.get("worktree_path") or "")
        model = "Unknown Codex model"
        first_at: str | None = None
        last_at: str | None = None
        latest: dict[str, Any] = {}
        try:
            with path.open(errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    stamp = record.get("timestamp")
                    if stamp:
                        first_at = first_at or str(stamp)
                        last_at = str(stamp)
                    payload = record.get("payload") or {}
                    if record.get("type") == "session_meta":
                        cwd = str(payload.get("cwd") or cwd)
                    elif record.get("type") == "turn_context":
                        model = str(payload.get("model") or model)
                    elif record.get("type") == "event_msg" and payload.get("type") == "token_count":
                        latest = (payload.get("info") or {}).get("total_token_usage") or latest
        except OSError:
            return None
        if not latest and model == "Unknown Codex model":
            return None
        return SessionUsage(
            session_id=session_id,
            provider="codex",
            model=model,
            issue_number=base.get("issue_number"),
            job_id=base.get("job_id"),
            role=str(base.get("role") or "session"),
            workspace=cwd,
            started_at=first_at,
            ended_at=last_at,
            status=str(base.get("status") or "unknown"),
            input_tokens=int(latest.get("input_tokens") or 0),
            cached_input_tokens=int(latest.get("cached_input_tokens") or 0),
            cache_write_input_tokens=int(latest.get("cache_write_input_tokens") or 0),
            output_tokens=int(latest.get("output_tokens") or 0),
            reasoning_tokens=int(latest.get("reasoning_output_tokens") or 0),
            total_tokens=int(latest.get("total_tokens") or 0),
        )

    def _parse_claude(
        self, path: Path, session_id: str, base: dict[str, Any]
    ) -> SessionUsage | None:
        cwd = str(base.get("worktree_path") or "")
        first_at: str | None = None
        last_at: str | None = None
        messages: dict[str, tuple[str, dict[str, Any]]] = {}
        try:
            with path.open(errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = str(record.get("cwd") or cwd)
                    stamp = record.get("timestamp")
                    if stamp:
                        first_at = first_at or str(stamp)
                        last_at = str(stamp)
                    if record.get("type") != "assistant":
                        continue
                    message = record.get("message") or {}
                    message_id = str(message.get("id") or f"line:{len(messages)}")
                    usage = message.get("usage") or {}
                    messages[message_id] = (str(message.get("model") or "Unknown Claude model"), usage)
        except OSError:
            return None
        if not messages:
            return None
        models = sorted({model for model, _ in messages.values()})
        input_tokens = sum(int(usage.get("input_tokens") or 0) for _, usage in messages.values())
        cached = sum(int(usage.get("cache_read_input_tokens") or 0) for _, usage in messages.values())
        cache_write = sum(
            int(usage.get("cache_creation_input_tokens") or 0) for _, usage in messages.values()
        )
        output = sum(int(usage.get("output_tokens") or 0) for _, usage in messages.values())
        return SessionUsage(
            session_id=session_id,
            provider="claude",
            model=", ".join(models),
            issue_number=base.get("issue_number"),
            job_id=base.get("job_id"),
            role=str(base.get("role") or "session"),
            workspace=cwd,
            started_at=first_at,
            ended_at=last_at,
            status=str(base.get("status") or "unknown"),
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_input_tokens=cache_write,
            output_tokens=output,
            total_tokens=input_tokens + cached + cache_write + output,
        )

    def _planner_sessions(self) -> list[SessionUsage]:
        path = self.controller_db.parent / "planner-telemetry.jsonl"
        if not path.exists():
            return []
        usages: list[SessionUsage] = []
        try:
            handle = path.open(errors="replace")
        except OSError:
            return []
        with handle:
            for index, line in enumerate(handle):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("kind") != "planner.usage":
                    continue
                issue_number = record.get("issue_number")
                if not isinstance(issue_number, int):
                    continue
                raw_models = record.get("model_usage") or {}
                usage = record.get("usage") or {}
                provider = str(record.get("provider") or "claude")
                reported_model = str(record.get("model") or f"{provider.title()} (not reported)")
                started_at = str(record.get("started_at") or record.get("at") or "") or None
                duration_ms = int(record.get("duration_ms") or 0)
                ended_at = None
                if started_at and duration_ms:
                    started_seconds = _iso_to_seconds(started_at)
                    if started_seconds is not None:
                        ended_at = datetime.fromtimestamp(
                            started_seconds + duration_ms / 1000, timezone.utc
                        ).isoformat().replace("+00:00", "Z")
                session_id = str(record.get("session_id") or f"planner-{issue_number}-{index}")
                model_rows = (
                    [(str(model), values) for model, values in raw_models.items()]
                    if raw_models
                    else [(reported_model, usage)]
                )
                for model, values in model_rows:
                    input_tokens = int(
                        values.get("inputTokens", values.get("input_tokens", 0)) or 0
                    )
                    cached = int(
                        values.get(
                            "cacheReadInputTokens",
                            values.get(
                                "cache_read_input_tokens", values.get("cached_input_tokens", 0)
                            ),
                        )
                        or 0
                    )
                    cache_write = int(
                        values.get(
                            "cacheCreationInputTokens",
                            values.get("cache_creation_input_tokens", 0),
                        )
                        or 0
                    )
                    output = int(
                        values.get("outputTokens", values.get("output_tokens", 0)) or 0
                    )
                    reasoning = int(
                        values.get(
                            "reasoningOutputTokens",
                            values.get("reasoning_output_tokens", 0),
                        )
                        or 0
                    )
                    total = (
                        int(values.get("total_tokens") or input_tokens + output)
                        if provider == "codex"
                        else input_tokens + cached + cache_write + output
                    )
                    usages.append(
                        SessionUsage(
                            session_id=session_id,
                            provider=provider,
                            model=model,
                            issue_number=issue_number,
                            job_id=None,
                            role="planner",
                            workspace="controller-collected repository evidence",
                            started_at=started_at,
                            ended_at=ended_at,
                            status="completed",
                            input_tokens=input_tokens,
                            cached_input_tokens=cached,
                            cache_write_input_tokens=cache_write,
                            output_tokens=output,
                            reasoning_tokens=reasoning,
                            total_tokens=total,
                        )
                    )
        return usages

    def sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_file_index()
            workspace_map, pr_to_issue = self._workspace_attribution()
            usages: list[SessionUsage] = []
            for binding in self._bindings():
                session_id = str(binding["agent_session_id"])
                path = self._file_index.get(session_id)
                if not path:
                    continue
                attribution = workspace_map.get(str(binding["workspace_id"]), {}).copy()
                attribution.setdefault(
                    "issue_number",
                    self._infer_issue(str(binding.get("worktree_path") or ""), pr_to_issue),
                )
                attribution.setdefault("job_id", None)
                attribution.setdefault("role", "session")
                attribution.update(
                    {
                        "worktree_path": str(binding.get("worktree_path") or ""),
                        "status": (
                            "completed"
                            if binding.get("last_event_type") == "Stop"
                            else "active"
                            if attribution.get("job_state")
                            in {"launching", "running", "reviewing", "repairing"}
                            else "no stop event"
                        ),
                    }
                )
                try:
                    stat = path.stat()
                except OSError:
                    continue
                cached = self._cache.get(path)
                usage = cached[2] if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size) else None
                if usage is None:
                    provider = "codex" if ".codex" in path.parts else "claude"
                    if provider == "codex":
                        usage = self._parse_codex(path, session_id, attribution)
                    else:
                        usage = self._parse_claude(path, session_id, attribution)
                    self._cache[path] = (stat.st_mtime_ns, stat.st_size, usage)
                if usage:
                    usages.append(
                        replace(
                            usage,
                            issue_number=attribution.get("issue_number"),
                            job_id=attribution.get("job_id"),
                            role=str(attribution.get("role") or "session"),
                            status=str(attribution.get("status") or "unknown"),
                        )
                    )
            usages.extend(self._planner_sessions())
            return [usage.as_dict() for usage in usages]


class DashboardData:
    def __init__(self, config: ControllerConfig):
        self.config = config
        self.telemetry = TelemetryScanner(config.database_path)
        self._split_lock = threading.Lock()
        self._approval_lock = threading.Lock()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection

    @staticmethod
    def _event_text(row: sqlite3.Row) -> str:
        detail = _json(row["detail_json"], {})
        kind = str(row["kind"])
        subject = f"#{row['issue_number']}" if row["issue_number"] else "Controller"
        if kind in {"issue.transition", "job.transition"}:
            target = row["job_id"] or subject
            return f"{target}: {detail.get('from', '?')} → {detail.get('to', '?')}"
        if kind == "plan.saved":
            return f"{subject}: plan {str(detail.get('plan_hash', ''))[:12]} saved"
        if kind == "plan.invalidated":
            return f"{subject}: plan invalidated — {detail.get('reason', 'unknown reason')}"
        if kind == "plan.approved":
            return f"{subject}: plan approved by {detail.get('actor', 'operator')}"
        if kind == "job.repair_pushed":
            return f"{row['job_id']}: repair attempt {detail.get('attempt', '?')} pushed"
        if kind == "issue.reconcile_failed":
            return f"{subject}: reconciliation failed"
        return f"{subject}: {kind.replace('.', ' ')}"

    def _read_state(self) -> dict[str, Any]:
        connection = self._connect(self.config.database_path)
        issues = [dict(row) for row in connection.execute("SELECT * FROM issues ORDER BY number")]
        plans = {
            int(row["issue_number"]): dict(row)
            for row in connection.execute(
                """
                SELECT p.* FROM plans p
                JOIN issues i ON i.current_plan_hash = p.plan_hash
                """
            )
        }
        jobs_by_issue: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute("SELECT * FROM jobs ORDER BY issue_number, created_at"):
            job = dict(row)
            job["paths"] = _json(job.pop("paths_json", "[]"), [])
            job["contracts"] = _json(job.pop("contracts_json", "[]"), [])
            job["depends_on"] = _json(job.pop("depends_on_json", "[]"), [])
            jobs_by_issue[int(job["issue_number"])].append(job)
        leases = [
            dict(row)
            for row in connection.execute(
                """
                SELECT l.*, j.issue_number, j.state AS job_state, j.pr_number
                FROM lane_leases l JOIN jobs j ON j.job_id = l.job_id
                ORDER BY l.lane
                """
            )
        ]
        event_rows = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 180").fetchall()
        first_events = {
            int(row["issue_number"]): dict(row)
            for row in connection.execute(
                """
                SELECT issue_number, MIN(created_at) AS first_at, MAX(created_at) AS last_at,
                       COUNT(*) AS event_count
                FROM events WHERE issue_number IS NOT NULL GROUP BY issue_number
                """
            )
        }
        plan_stats = {
            int(row["issue_number"]): dict(row)
            for row in connection.execute(
                """
                SELECT issue_number, COUNT(*) AS plan_count,
                       SUM(CASE WHEN status='superseded' THEN 1 ELSE 0 END) AS invalidated_count
                FROM plans GROUP BY issue_number
                """
            )
        }
        splits: dict[int, dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT s.* FROM issue_splits s JOIN issues i
            ON i.number=s.parent_issue_number AND i.current_plan_hash=s.plan_hash
            ORDER BY s.parent_issue_number
            """
        ):
            split = dict(row)
            split["children"] = []
            splits[int(split["parent_issue_number"])] = split
        for row in connection.execute(
            """
            SELECT c.* FROM issue_split_children c JOIN issues i
            ON i.number=c.parent_issue_number AND i.current_plan_hash=c.plan_hash
            ORDER BY c.parent_issue_number, c.ordinal, c.job_id
            """
        ):
            child = dict(row)
            child["spec"] = _json(child.pop("spec_json", "{}"), {})
            split = splits.get(int(child["parent_issue_number"]))
            if split is not None:
                split["children"].append(child)
        sprints = [dict(row) for row in connection.execute("SELECT * FROM sprints ORDER BY number DESC")]
        sprint_items: dict[int, list[dict[str, Any]]] = defaultdict(list)
        latest_sprint_item: dict[int, dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT si.*, s.number AS sprint_number, s.title AS sprint_title,
                   s.state AS sprint_state, s.iteration_id, s.starts_at, s.ends_at,
                   s.closed_at
            FROM sprint_items si JOIN sprints s ON s.id=si.sprint_id
            ORDER BY s.number, si.rank, si.issue_number
            """
        ):
            item = dict(row)
            sprint_items[int(item["sprint_id"])].append(item)
            latest_sprint_item[int(item["issue_number"])] = item
        connection.close()
        return {
            "issues": issues,
            "plans": plans,
            "jobs_by_issue": jobs_by_issue,
            "leases": leases,
            "event_rows": event_rows,
            "first_events": first_events,
            "plan_stats": plan_stats,
            "splits": splits,
            "sprints": sprints,
            "sprint_items": sprint_items,
            "latest_sprint_item": latest_sprint_item,
        }

    def snapshot(self) -> dict[str, Any]:
        state = self._read_state()
        sessions = self.telemetry.sessions()
        sessions_by_issue: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for session in sessions:
            if session["issue_number"] is not None:
                sessions_by_issue[int(session["issue_number"])].append(session)

        cards: list[dict[str, Any]] = []
        state_counts: Counter[str] = Counter()
        for issue in state["issues"]:
            number = int(issue["number"])
            controller_state = str(issue["controller_state"])
            product_stage = str(issue.get("product_stage") or "legacy_active")
            if str(issue.get("github_state") or "").upper() == "CLOSED":
                column = "done"
            else:
                column = (
                    PRODUCT_TO_COLUMN.get(
                        product_stage, STATE_TO_COLUMN.get(controller_state, "blocked")
                    )
                    if self.config.github.project_number
                    else STATE_TO_COLUMN.get(controller_state, "blocked")
                )
            state_counts[column] += 1
            plan_row = state["plans"].get(number)
            plan = _json(plan_row.get("plan_json") if plan_row else None, {})
            issue_sessions = sessions_by_issue.get(number, [])
            token_totals = {
                key: sum(int(session.get(key) or 0) for session in issue_sessions)
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                )
            }
            model_names = sorted({session["model"] for session in issue_sessions})
            event_meta = state["first_events"].get(number, {})
            stats = state["plan_stats"].get(number, {})
            sprint_item = state["latest_sprint_item"].get(number)
            story_points = (
                plan.get("story_points")
                if plan
                else sprint_item.get("story_points") if sprint_item else None
            )
            cards.append(
                {
                    "number": number,
                    "title": issue["title"],
                    "github_state": issue["github_state"],
                    "state": controller_state,
                    "product_stage": product_stage,
                    "column": column,
                    "url": issue["url"],
                    "labels": _json(issue["labels_json"], []),
                    "observed_at": issue["observed_at"],
                    "updated_at": issue["updated_at"],
                    "age_seconds": _duration_seconds(event_meta.get("first_at") or issue["observed_at"]),
                    "event_count": int(event_meta.get("event_count") or 0),
                    "plan": (
                        {
                            "hash": plan_row["plan_hash"],
                            "short_hash": plan_row["plan_hash"][:12],
                            "base_sha": plan_row["base_sha"],
                            "status": plan_row["status"],
                            "created_at": plan_row["created_at"],
                            "summary": plan.get("summary"),
                            "risk": plan.get("risk"),
                            "job_count": len(plan.get("jobs") or []),
                            "story_points": plan.get("story_points"),
                            "points_evidence": plan.get("points_evidence"),
                            "issue_dependencies": plan.get("issue_dependencies") or [],
                            "proposed_jobs": [
                                {
                                    "id": job.get("id"),
                                    "lane": job.get("lane"),
                                    "title": job.get("title"),
                                    "paths": job.get("paths") or [],
                                    "verify": job.get("verify"),
                                    "contracts_read": job.get("contracts_read") or [],
                                    "contracts_changed": job.get("contracts_changed") or [],
                                    "depends_on": job.get("depends_on") or [],
                                    "acceptance": job.get("acceptance") or [],
                                }
                                for job in plan.get("jobs") or []
                            ],
                        }
                        if plan_row
                        else None
                    ),
                    "plan_count": int(stats.get("plan_count") or 0),
                    "invalidated_count": int(stats.get("invalidated_count") or 0),
                    "jobs": state["jobs_by_issue"].get(number, []),
                    "split": state["splits"].get(number),
                    "sessions": issue_sessions,
                    "tokens": token_totals,
                    "models": model_names,
                    "story_points": story_points,
                    "tokens_per_point": (
                        token_totals["total_tokens"] / int(story_points)
                        if story_points
                        else None
                    ),
                    "points_evidence": (
                        plan.get("points_evidence")
                        if plan
                        else sprint_item.get("points_evidence") if sprint_item else None
                    ),
                    "sprint": sprint_item,
                    "planner_telemetry": (
                        "This plan predates planner telemetry; its model and token usage are unavailable"
                        if plan_row and not any(
                            session.get("role") == "planner" for session in issue_sessions
                        )
                        else None
                    ),
                }
            )

        token_fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
        token_totals = {
            key: sum(int(session.get(key) or 0) for session in sessions) for key in token_fields
        }
        models: dict[str, dict[str, Any]] = {}
        for session in sessions:
            model = session["model"]
            bucket = models.setdefault(
                model,
                {
                    "model": model,
                    "provider": session["provider"],
                    "sessions": 0,
                    "issues": set(),
                    **{key: 0 for key in token_fields},
                },
            )
            bucket["sessions"] += 1
            if session["issue_number"] is not None:
                bucket["issues"].add(int(session["issue_number"]))
            for key in token_fields:
                bucket[key] += int(session.get(key) or 0)
        model_rows = []
        for bucket in models.values():
            bucket["issues"] = len(bucket["issues"])
            model_rows.append(bucket)
        model_rows.sort(key=lambda item: item["total_tokens"], reverse=True)

        sprint_rows: list[dict[str, Any]] = []
        for sprint in state["sprints"]:
            items = state["sprint_items"].get(int(sprint["id"]), [])
            start = _iso_to_seconds(sprint.get("starts_at")) or 0
            finish = _iso_to_seconds(sprint.get("closed_at")) or time.time()
            sprint_sessions = [
                session
                for session in sessions
                if session.get("issue_number") in {item["issue_number"] for item in items}
                and start
                <= (_iso_to_seconds(session.get("started_at") or session.get("ended_at")) or 0)
                <= finish
            ]
            sprint_tokens = {
                key: sum(int(session.get(key) or 0) for session in sprint_sessions)
                for key in token_fields
            }
            completed_points = sum(
                int(item.get("story_points") or 0)
                for item in items
                if item.get("status") == "done"
            )
            committed_points = sum(
                int(item.get("story_points") or 0)
                for item in items
                if item.get("commitment") == "committed"
            )
            added_points = sum(
                int(item.get("story_points") or 0)
                for item in items
                if item.get("commitment") == "added"
            )
            sprint_rows.append(
                {
                    **sprint,
                    "items": items,
                    "committed_points": committed_points,
                    "added_points": added_points,
                    "completed_points": completed_points,
                    "carryover_points": sum(
                        int(item.get("story_points") or 0)
                        for item in items
                        if item.get("status") not in {"done", "active"}
                    ),
                    "tokens": sprint_tokens,
                    "tokens_per_completed_point": (
                        sprint_tokens["total_tokens"] / completed_points
                        if completed_points
                        else None
                    ),
                    "overdue": bool(
                        sprint.get("state") == "active"
                        and (_iso_to_seconds(sprint.get("ends_at")) or float("inf")) < time.time()
                    ),
                }
            )

        event_items = [
            {
                "id": int(row["id"]),
                "kind": row["kind"],
                "issue_number": row["issue_number"],
                "job_id": row["job_id"],
                "text": self._event_text(row),
                "created_at": row["created_at"],
            }
            for row in state["event_rows"]
        ]

        runtime = _controller_runtime()
        runtime["max_parallel_planners"] = self.config.max_parallel_planners
        runtime["auto_replan_max_attempts"] = self.config.auto_replan_max_attempts
        superset_running, superset_pid = _pid_is_running(
            Path("~/.superset/terminal-host.pid").expanduser()
        )
        latest_event = event_items[0]["created_at"] if event_items else None
        measured_plans = len(
            {
                session["session_id"]
                for session in sessions
                if session.get("role") == "planner"
            }
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "repo": self.config.github.repo,
            "project_number": self.config.github.project_number,
            "columns": [
                {"id": column, "label": label, "count": state_counts[column]}
                for column, label in KANBAN_COLUMNS
            ],
            "issues": cards,
            "leases": state["leases"],
            "events": event_items,
            "sessions": sessions,
            "analytics": {
                "tokens": token_totals,
                "models": model_rows,
                "measured_sessions": len(sessions),
                "measured_plans": measured_plans,
                "measured_issues": len(sessions_by_issue),
                "tracked_issues": len(cards),
                "open_issues": sum(1 for card in cards if card["github_state"] == "OPEN"),
                "plan_invalidations": sum(card["invalidated_count"] for card in cards),
                "active_jobs": sum(
                    1
                    for card in cards
                    for job in card["jobs"]
                    if job["state"]
                    in {"launching", "running", "pr_open", "reviewing", "repairing"}
                ),
                "blocked_jobs": sum(
                    1
                    for card in cards
                    for job in card["jobs"]
                    if job["state"] in {"blocked", "quarantined"}
                ),
                "sprints": sprint_rows,
                "active_sprint": next(
                    (sprint for sprint in sprint_rows if sprint["state"] == "active"),
                    None,
                ),
                "telemetry_note": (
                    "Build and review totals come from persisted Codex/Claude session usage. "
                    + (
                        f"Planner telemetry is exact for {measured_plans} instrumented run(s); "
                        "older planner runs predate instrumentation and remain excluded."
                        if measured_plans
                        else "Planner instrumentation is active; no post-instrumentation planner run "
                        "has completed yet. Older planning usage is excluded rather than estimated."
                    )
                ),
            },
            "runtime": {
                "controller": runtime,
                "superset": {"running": superset_running, "pid": superset_pid},
                "latest_event_at": latest_event,
                "latest_event_age_seconds": _duration_seconds(latest_event),
                "database": str(self.config.database_path),
                "read_only": not bool(self.config.github.project_number),
                "controlled_actions": (
                    ["start_sprint", "split_issue", "approve_plan"]
                    if self.config.github.project_number
                    else []
                ),
            },
        }

    def start_sprint(
        self,
        *,
        title: str | None = None,
        duration_days: int | None = None,
    ) -> dict[str, Any]:
        database = Database(self.config.database_path)
        try:
            project = ProjectBoard(self.config.github)
            return SprintManager(
                self.config,
                database,
                project,
                GitHubClient(self.config.github),
            ).start(title=title, duration_days=duration_days)
        finally:
            database.close()

    def split_issue(self, issue_number: int) -> dict[str, Any]:
        if not self._split_lock.acquire(blocking=False):
            raise RuntimeError("another issue split is already running")
        database = Database(self.config.database_path)
        try:
            return IssueSplitter(
                self.config,
                database,
                ProjectBoard(self.config.github),
                GitHubClient(self.config.github),
            ).split(issue_number)
        finally:
            database.close()
            self._split_lock.release()

    def approve_plan(self, issue_number: int, plan_hash: str) -> dict[str, Any]:
        if not self.config.github.project_number:
            raise RuntimeError("dashboard approval requires a configured GitHub project")
        if PLAN_HASH_RE.fullmatch(plan_hash) is None:
            raise ValueError("plan approval requires the full 64-character lowercase hash")
        if not self._approval_lock.acquire(blocking=False):
            raise RuntimeError("another plan approval is already being submitted")
        database = Database(self.config.database_path)
        try:
            issue = database.get_issue(issue_number)
            if issue is None:
                raise ValueError(f"issue #{issue_number} is not tracked")
            if str(issue.get("github_state") or "").upper() != "OPEN":
                raise ValueError(f"issue #{issue_number} is closed")
            current = database.current_plan(issue_number)
            if current is None:
                raise ValueError(f"issue #{issue_number} has no current plan")
            if current["plan_hash"] != plan_hash:
                raise ValueError(
                    "this plan is stale; refresh the dashboard and review the current plan"
                )
            if current["status"] != "awaiting_approval":
                raise ValueError(
                    f"plan {plan_hash[:12]} is {current['status'].replace('_', ' ')}, not awaiting approval"
                )
            if issue["controller_state"] != "awaiting_approval":
                raise ValueError(
                    f"issue #{issue_number} is {str(issue['controller_state']).replace('_', ' ')}, not awaiting approval"
                )
            if int(current["plan"].get("story_points") or 0) == 21:
                raise ValueError("21-point plans must be split before they can be approved")
            return GitHubClient(self.config.github).post_plan_approval(
                issue_number, plan_hash
            )
        finally:
            database.close()
            self._approval_lock.release()


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], data: DashboardData, html: bytes):
        self.dashboard_data = data
        self.dashboard_html = html
        self.action_token = secrets.token_urlsafe(32)
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            body = self.server.dashboard_html
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/api/snapshot":
            try:
                payload = self.server.dashboard_data.snapshot()
                payload.setdefault("runtime", {})["action_token"] = self.server.action_token
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
                status = HTTPStatus.OK
            except Exception as exc:  # keep the local observer alive on transient WAL reads
                body = json.dumps(
                    {"error": str(exc), "generated_at": datetime.now(timezone.utc).isoformat()}
                ).encode()
                status = HTTPStatus.SERVICE_UNAVAILABLE
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/healthz":
            body = b'{"ok":true,"controlled_actions":["start_sprint","split_issue","approve_plan"]}'
            self._headers(HTTPStatus.OK, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        body = b"Not found"
        self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        split_match = SPLIT_ACTION_RE.fullmatch(path)
        approve_match = APPROVE_ACTION_RE.fullmatch(path)
        if path != "/api/sprints/start" and split_match is None and approve_match is None:
            body = b'{"error":"unsupported action"}'
            self._headers(HTTPStatus.METHOD_NOT_ALLOWED, "application/json", len(body))
            self.wfile.write(body)
            return
        origin = self.headers.get("Origin", "")
        if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
            body = b'{"error":"invalid origin"}'
            self._headers(HTTPStatus.FORBIDDEN, "application/json", len(body))
            self.wfile.write(body)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 8192:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if payload.get("token") != self.server.action_token:
                raise PermissionError("invalid dashboard action token")
            if split_match is not None:
                result = self.server.dashboard_data.split_issue(int(split_match.group(1)))
                body = json.dumps({"split": result}, separators=(",", ":")).encode()
                status = HTTPStatus.OK
            elif approve_match is not None:
                result = self.server.dashboard_data.approve_plan(
                    int(approve_match.group(1)),
                    str(payload.get("plan_hash") or ""),
                )
                body = json.dumps({"approval": result}, separators=(",", ":")).encode()
                status = HTTPStatus.CREATED if result.get("created") else HTTPStatus.OK
            else:
                result = self.server.dashboard_data.start_sprint(
                    title=str(payload.get("title") or "").strip() or None,
                    duration_days=(
                        int(payload["duration_days"])
                        if payload.get("duration_days") is not None
                        else None
                    ),
                )
                body = json.dumps({"sprint": result}, separators=(",", ":")).encode()
                status = HTTPStatus.CREATED
        except PermissionError as exc:
            body = json.dumps({"error": str(exc)}).encode()
            status = HTTPStatus.FORBIDDEN
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode()
            status = HTTPStatus.BAD_REQUEST
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)


def serve_dashboard(
    config: ControllerConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 7331,
    open_browser: bool = False,
) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dashboard may only bind to a loopback address")
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    html = html_path.read_bytes()
    server = DashboardHTTPServer((host, port), DashboardData(config), html)
    url = f"http://{host}:{server.server_address[1]}"
    print(
        json.dumps(
            {
                "dashboard": url,
                "controlled_actions": ["start_sprint", "split_issue", "approve_plan"],
            }
        ),
        flush=True,
    )
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
