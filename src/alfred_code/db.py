from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .states import ISSUE_STATES, JOB_STATES
from .util import canonical_json, utcnow


SCHEMA_VERSION = 3


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY,
    node_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    body_hash TEXT NOT NULL,
    github_state TEXT NOT NULL,
    controller_state TEXT NOT NULL,
    url TEXT,
    labels_json TEXT NOT NULL DEFAULT '[]',
    current_plan_hash TEXT,
    observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_hash TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL REFERENCES issues(number),
    base_sha TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT
);
CREATE INDEX IF NOT EXISTS plans_issue_idx ON plans(issue_number, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    plan_hash TEXT PRIMARY KEY REFERENCES plans(plan_hash),
    issue_number INTEGER NOT NULL REFERENCES issues(number),
    actor TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    comment_url TEXT,
    approved_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL REFERENCES issues(number),
    plan_hash TEXT NOT NULL REFERENCES plans(plan_hash),
    lane TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    branch TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    verify_command TEXT NOT NULL,
    contracts_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    workspace_id TEXT,
    workspace_url TEXT,
    agent_id TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    head_sha TEXT,
    review_sha TEXT,
    review_workspace_id TEXT,
    review_agent_id TEXT,
    review_requested_at TEXT,
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    repair_sha TEXT,
    repair_agent_id TEXT,
    repair_requested_at TEXT,
    repair_token TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(issue_number, plan_hash, lane)
);
CREATE INDEX IF NOT EXISTS jobs_issue_idx ON jobs(issue_number, created_at);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs(state, updated_at);

CREATE TABLE IF NOT EXISTS lane_leases (
    lane TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    authority TEXT NOT NULL,
    object_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(authority, object_key)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    issue_number INTEGER,
    job_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_issue_idx ON events(issue_number, id);

CREATE TABLE IF NOT EXISTS notifications (
    dedupe_key TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_imports (
    source_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY(source_path, content_hash)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        self.connection.executescript(SCHEMA)
        with self.transaction():
            row = self.connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                self.connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] in {1, 2}:
                columns = {
                    item["name"] for item in self.connection.execute("PRAGMA table_info(jobs)")
                }
                for name in ("review_workspace_id", "review_agent_id", "review_requested_at"):
                    if name not in columns:
                        self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
                repair_columns = {
                    "repair_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "repair_sha": "TEXT",
                    "repair_agent_id": "TEXT",
                    "repair_requested_at": "TEXT",
                    "repair_token": "TEXT",
                }
                for name, declaration in repair_columns.items():
                    if name not in columns:
                        self.connection.execute(
                            f"ALTER TABLE jobs ADD COLUMN {name} {declaration}"
                        )
                self.connection.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {row['version']} is not supported by controller {SCHEMA_VERSION}"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def event(
        self,
        kind: str,
        detail: dict[str, Any],
        *,
        issue_number: int | None = None,
        job_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        conn = connection or self.connection
        conn.execute(
            "INSERT INTO events(kind, issue_number, job_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, issue_number, job_id, canonical_json(detail), utcnow()),
        )

    def upsert_issue(self, issue: dict[str, Any]) -> dict[str, Any]:
        number = int(issue["number"])
        now = utcnow()
        body = str(issue.get("body") or "")
        labels = issue.get("labels") or []
        label_names = [x.get("name", "") if isinstance(x, dict) else str(x) for x in labels]
        from .util import content_hash

        with self.transaction() as conn:
            previous = conn.execute("SELECT * FROM issues WHERE number = ?", (number,)).fetchone()
            initial_state = "closed" if str(issue.get("state", "OPEN")).upper() == "CLOSED" else "observed"
            conn.execute(
                """
                INSERT INTO issues(number, node_id, title, body, body_hash, github_state,
                                   controller_state, url, labels_json, observed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(number) DO UPDATE SET
                    node_id=excluded.node_id, title=excluded.title, body=excluded.body,
                    body_hash=excluded.body_hash, github_state=excluded.github_state,
                    url=excluded.url, labels_json=excluded.labels_json,
                    observed_at=excluded.observed_at, updated_at=excluded.updated_at
                """,
                (
                    number,
                    issue.get("id") or issue.get("node_id"),
                    str(issue.get("title") or ""),
                    body,
                    content_hash(body),
                    str(issue.get("state") or "OPEN").upper(),
                    initial_state,
                    issue.get("url"),
                    canonical_json(label_names),
                    now,
                    now,
                ),
            )
            if previous is None:
                self.event("issue.observed", {"title": issue.get("title", "")}, issue_number=number, connection=conn)
        return self.get_issue(number) or {}

    def get_issue(self, number: int) -> dict[str, Any] | None:
        return self._dict(self.connection.execute("SELECT * FROM issues WHERE number = ?", (number,)).fetchone())

    def list_issues(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM issues ORDER BY number")]

    def set_issue_state(self, number: int, state: str, detail: dict[str, Any] | None = None) -> None:
        if state not in ISSUE_STATES:
            raise ValueError(f"unknown issue state: {state}")
        with self.transaction() as conn:
            row = conn.execute("SELECT controller_state FROM issues WHERE number = ?", (number,)).fetchone()
            if row is None:
                raise KeyError(f"issue #{number} not found")
            if row["controller_state"] == state:
                return
            conn.execute(
                "UPDATE issues SET controller_state = ?, updated_at = ? WHERE number = ?",
                (state, utcnow(), number),
            )
            self.event(
                "issue.transition",
                {"from": row["controller_state"], "to": state, **(detail or {})},
                issue_number=number,
                connection=conn,
            )

    def save_plan(self, issue_number: int, plan_hash: str, plan: dict[str, Any]) -> None:
        now = utcnow()
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT current_plan_hash FROM issues WHERE number = ?", (issue_number,)
            ).fetchone()
            if current is None:
                raise KeyError(f"issue #{issue_number} not found")
            old_hash = current["current_plan_hash"]
            if old_hash and old_hash != plan_hash:
                conn.execute(
                    "UPDATE plans SET status = 'superseded', superseded_at = ? WHERE plan_hash = ?",
                    (now, old_hash),
                )
                conn.execute("UPDATE approvals SET revoked_at = ? WHERE plan_hash = ?", (now, old_hash))
            conn.execute(
                """
                INSERT INTO plans(plan_hash, issue_number, base_sha, plan_json, status, created_at)
                VALUES (?, ?, ?, ?, 'awaiting_approval', ?)
                ON CONFLICT(plan_hash) DO NOTHING
                """,
                (plan_hash, issue_number, plan["base_sha"], canonical_json(plan), now),
            )
            conn.execute(
                "UPDATE issues SET current_plan_hash = ?, controller_state = 'awaiting_approval', updated_at = ? WHERE number = ?",
                (plan_hash, now, issue_number),
            )
            self.event(
                "plan.saved",
                {"plan_hash": plan_hash, "supersedes": old_hash if old_hash != plan_hash else None},
                issue_number=issue_number,
                connection=conn,
            )

    def invalidate_plan(self, issue_number: int, reason: str) -> None:
        now = utcnow()
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT current_plan_hash FROM issues WHERE number = ?", (issue_number,)
            ).fetchone()
            if current is None or not current["current_plan_hash"]:
                return
            plan_hash = current["current_plan_hash"]
            conn.execute(
                "UPDATE plans SET status='superseded', superseded_at=? WHERE plan_hash=?",
                (now, plan_hash),
            )
            conn.execute("UPDATE approvals SET revoked_at=? WHERE plan_hash=?", (now, plan_hash))
            conn.execute(
                "UPDATE issues SET current_plan_hash=NULL, controller_state='planning', updated_at=? WHERE number=?",
                (now, issue_number),
            )
            self.event(
                "plan.invalidated",
                {"plan_hash": plan_hash, "reason": reason},
                issue_number=issue_number,
                connection=conn,
            )

    def current_plan(self, issue_number: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT p.* FROM plans p
            JOIN issues i ON i.current_plan_hash = p.plan_hash
            WHERE i.number = ?
            """,
            (issue_number,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        return result

    def record_approval(
        self,
        issue_number: int,
        plan_hash: str,
        actor: str,
        comment_id: str,
        comment_url: str | None,
        approved_at: str,
    ) -> bool:
        now = utcnow()
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT current_plan_hash FROM issues WHERE number = ?", (issue_number,)
            ).fetchone()
            if current is None or current["current_plan_hash"] != plan_hash:
                return False
            conn.execute(
                """
                INSERT INTO approvals(plan_hash, issue_number, actor, comment_id, comment_url, approved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_hash) DO UPDATE SET actor=excluded.actor,
                    comment_id=excluded.comment_id, comment_url=excluded.comment_url,
                    approved_at=excluded.approved_at, revoked_at=NULL
                """,
                (plan_hash, issue_number, actor, comment_id, comment_url, approved_at),
            )
            conn.execute("UPDATE plans SET status = 'approved' WHERE plan_hash = ?", (plan_hash,))
            conn.execute(
                "UPDATE issues SET controller_state = 'approved', updated_at = ? WHERE number = ?",
                (now, issue_number),
            )
            self.event(
                "plan.approved",
                {"plan_hash": plan_hash, "actor": actor, "comment_id": comment_id},
                issue_number=issue_number,
                connection=conn,
            )
        return True

    def is_approved(self, plan_hash: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM approvals WHERE plan_hash = ? AND revoked_at IS NULL", (plan_hash,)
        ).fetchone()
        return row is not None

    def reject_plan(
        self,
        issue_number: int,
        plan_hash: str,
        actor: str,
        comment_id: str,
        comment_url: str | None,
        rejected_at: str,
    ) -> bool:
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT current_plan_hash FROM issues WHERE number = ?", (issue_number,)
            ).fetchone()
            if current is None or current["current_plan_hash"] != plan_hash:
                return False
            conn.execute("UPDATE plans SET status = 'rejected' WHERE plan_hash = ?", (plan_hash,))
            conn.execute(
                "UPDATE issues SET controller_state = 'blocked', updated_at = ? WHERE number = ?",
                (utcnow(), issue_number),
            )
            self.event(
                "plan.rejected",
                {
                    "plan_hash": plan_hash,
                    "actor": actor,
                    "comment_id": comment_id,
                    "comment_url": comment_url,
                    "rejected_at": rejected_at,
                },
                issue_number=issue_number,
                connection=conn,
            )
        return True

    def materialize_jobs(self, issue_number: int, plan_hash: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.is_approved(plan_hash):
            raise RuntimeError(f"plan {plan_hash[:12]} is not approved")
        now = utcnow()
        with self.transaction() as conn:
            for job in plan["jobs"]:
                conn.execute(
                    """
                    INSERT INTO jobs(job_id, issue_number, plan_hash, lane, title, state, branch,
                                     paths_json, verify_command, contracts_json, depends_on_json,
                                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO NOTHING
                    """,
                    (
                        job["id"],
                        issue_number,
                        plan_hash,
                        job["lane"],
                        job["title"],
                        job["branch"],
                        canonical_json(job["paths"]),
                        job["verify"],
                        canonical_json(
                            {
                                "read": job.get("contracts_read", []),
                                "changed": job.get("contracts_changed", []),
                            }
                        ),
                        canonical_json(job.get("depends_on", [])),
                        now,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE issues SET controller_state = 'building', updated_at = ? WHERE number = ?",
                (now, issue_number),
            )
            self.event(
                "jobs.materialized",
                {"plan_hash": plan_hash, "count": len(plan["jobs"])},
                issue_number=issue_number,
                connection=conn,
            )
        return self.list_jobs(issue_number)

    @staticmethod
    def decode_job(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["paths"] = json.loads(result.pop("paths_json"))
        result["contracts"] = json.loads(result.pop("contracts_json"))
        result["depends_on"] = json.loads(result.pop("depends_on_json"))
        return result

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self.decode_job(row) if row else None

    def list_jobs(self, issue_number: int | None = None) -> list[dict[str, Any]]:
        if issue_number is None:
            rows = self.connection.execute("SELECT * FROM jobs ORDER BY created_at, job_id")
        else:
            rows = self.connection.execute(
                "SELECT * FROM jobs WHERE issue_number = ? ORDER BY created_at, job_id", (issue_number,)
            )
        return [self.decode_job(row) for row in rows]

    def update_job(self, job_id: str, *, state: str | None = None, **fields: Any) -> dict[str, Any]:
        if state is not None and state not in JOB_STATES:
            raise ValueError(f"unknown job state: {state}")
        allowed = {
            "workspace_id",
            "workspace_url",
            "agent_id",
            "pr_number",
            "pr_url",
            "head_sha",
            "review_sha",
            "review_workspace_id",
            "review_agent_id",
            "review_requested_at",
            "repair_attempts",
            "repair_sha",
            "repair_agent_id",
            "repair_requested_at",
            "repair_token",
            "last_error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {', '.join(sorted(unknown))}")
        with self.transaction() as conn:
            previous = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if previous is None:
                raise KeyError(f"job {job_id} not found")
            updates = dict(fields)
            if state is not None:
                updates["state"] = state
            updates["updated_at"] = utcnow()
            assignments = ", ".join(f"{name} = ?" for name in updates)
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?",
                (*updates.values(), job_id),
            )
            if state is not None and state != previous["state"]:
                self.event(
                    "job.transition",
                    {"from": previous["state"], "to": state},
                    issue_number=previous["issue_number"],
                    job_id=job_id,
                    connection=conn,
                )
        return self.get_job(job_id) or {}

    def acquire_lane(self, lane: str, job_id: str) -> bool:
        now = utcnow()
        with self.transaction() as conn:
            owned = conn.execute("SELECT job_id FROM lane_leases WHERE lane = ?", (lane,)).fetchone()
            if owned and owned["job_id"] != job_id:
                return False
            conn.execute(
                """
                INSERT INTO lane_leases(lane, job_id, acquired_at, heartbeat_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lane) DO UPDATE SET heartbeat_at=excluded.heartbeat_at
                """,
                (lane, job_id, now, now),
            )
        return True

    def release_lane(self, job_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM lane_leases WHERE job_id = ?", (job_id,))

    def lease_owner(self, lane: str) -> str | None:
        row = self.connection.execute("SELECT job_id FROM lane_leases WHERE lane = ?", (lane,)).fetchone()
        return str(row["job_id"]) if row else None

    def observe(self, authority: str, object_key: str, value: Any) -> None:
        now = utcnow()
        self.connection.execute(
            """
            INSERT INTO observations(authority, object_key, value_json, observed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(authority, object_key) DO UPDATE SET
                value_json=excluded.value_json, observed_at=excluded.observed_at
            """,
            (authority, object_key, canonical_json(value), now),
        )

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def claim_notification(self, dedupe_key: str, channel: str, payload: dict[str, Any]) -> bool:
        now = utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, attempts FROM notifications WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if row and (row["status"] == "sent" or row["attempts"] >= 5):
                return False
            if row:
                conn.execute(
                    "UPDATE notifications SET status='sending', attempts=attempts+1, updated_at=? WHERE dedupe_key=?",
                    (now, dedupe_key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO notifications(dedupe_key, channel, status, payload_json, attempts, created_at, updated_at)
                    VALUES (?, ?, 'sending', ?, 1, ?, ?)
                    """,
                    (dedupe_key, channel, canonical_json(payload), now, now),
                )
        return True

    def finish_notification(self, dedupe_key: str, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE notifications SET status=?, last_error=?, updated_at=? WHERE dedupe_key=?",
            ("failed" if error else "sent", error, utcnow(), dedupe_key),
        )
