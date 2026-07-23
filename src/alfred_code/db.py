from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .states import (
    ACTIVE_LEASE_STATES,
    ISSUE_STATES,
    JOB_STATES,
    PRODUCT_STAGES,
    SPRINT_ITEM_STATES,
)
from .util import canonical_json, utcnow


SCHEMA_VERSION = 7


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
    product_stage TEXT NOT NULL DEFAULT 'backlog',
    project_rank INTEGER,
    carryover_replan INTEGER NOT NULL DEFAULT 0 CHECK (carryover_replan IN (0, 1)),
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
    base_sha TEXT,
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
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_issue_idx ON jobs(issue_number, created_at);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs(state, updated_at);

CREATE TABLE IF NOT EXISTS lane_leases (
    lane TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    iteration_id TEXT,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sprints_state_idx ON sprints(state, number);

CREATE TABLE IF NOT EXISTS sprint_items (
    sprint_id INTEGER NOT NULL REFERENCES sprints(id),
    issue_number INTEGER NOT NULL REFERENCES issues(number),
    rank INTEGER NOT NULL,
    commitment TEXT NOT NULL,
    status TEXT NOT NULL,
    story_points INTEGER,
    points_evidence TEXT,
    added_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY(sprint_id, issue_number)
);
CREATE INDEX IF NOT EXISTS sprint_items_issue_idx ON sprint_items(issue_number, sprint_id);

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
        row = self.connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            with self.transaction():
                self.connection.execute(
                    "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            return
        if row["version"] not in {1, 2, 3, 4, 5, 6, SCHEMA_VERSION}:
            raise RuntimeError(
                f"database schema {row['version']} is not supported by controller {SCHEMA_VERSION}"
            )
        if row["version"] == SCHEMA_VERSION:
            return

        with self.transaction():
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
            if "base_sha" not in columns:
                self.connection.execute("ALTER TABLE jobs ADD COLUMN base_sha TEXT")

            issue_columns = {
                item["name"] for item in self.connection.execute("PRAGMA table_info(issues)")
            }
            if "product_stage" not in issue_columns:
                self.connection.execute(
                    "ALTER TABLE issues ADD COLUMN product_stage TEXT NOT NULL DEFAULT 'backlog'"
                )
                self.connection.execute(
                    """
                    UPDATE issues SET product_stage = CASE
                        WHEN controller_state IN ('completed', 'closed') THEN 'done'
                        WHEN controller_state = 'observed' THEN 'backlog'
                        WHEN controller_state IN ('building', 'ready_merge') THEN 'legacy_active'
                        ELSE 'inbox'
                    END
                    """
                )
            if "project_rank" not in issue_columns:
                self.connection.execute("ALTER TABLE issues ADD COLUMN project_rank INTEGER")
            if "carryover_replan" not in issue_columns:
                self.connection.execute(
                    "ALTER TABLE issues ADD COLUMN carryover_replan "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (carryover_replan IN (0, 1))"
                )

        if self._jobs_has_lane_uniqueness():
            self._remove_jobs_lane_uniqueness()
        with self.transaction():
            self.connection.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))

    def _jobs_has_lane_uniqueness(self) -> bool:
        expected = ["issue_number", "plan_hash", "lane"]
        for row in self.connection.execute("PRAGMA index_list(jobs)"):
            if not row["unique"]:
                continue
            columns = [
                item["name"]
                for item in self.connection.execute(
                    f"PRAGMA index_info({json.dumps(row['name'])})"
                )
            ]
            if columns == expected:
                return True
        return False

    def _remove_jobs_lane_uniqueness(self) -> None:
        """Rebuild v4 jobs without discarding job, repair, or lease history."""
        leases = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT lane, job_id, acquired_at, heartbeat_at FROM lane_leases"
            )
        ]
        columns = [row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")]
        column_list = ", ".join(columns)
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.transaction() as conn:
                conn.execute("DROP TABLE lane_leases")
                conn.execute("ALTER TABLE jobs RENAME TO jobs_v4")
                conn.execute(
                    """
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        issue_number INTEGER NOT NULL REFERENCES issues(number),
                        plan_hash TEXT NOT NULL REFERENCES plans(plan_hash),
                        lane TEXT NOT NULL,
                        title TEXT NOT NULL,
                        state TEXT NOT NULL,
                        branch TEXT NOT NULL,
                        base_sha TEXT,
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
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    f"INSERT INTO jobs ({column_list}) SELECT {column_list} FROM jobs_v4"
                )
                conn.execute("DROP TABLE jobs_v4")
                conn.execute("CREATE INDEX jobs_issue_idx ON jobs(issue_number, created_at)")
                conn.execute("CREATE INDEX jobs_state_idx ON jobs(state, updated_at)")
                conn.execute(
                    """
                    CREATE TABLE lane_leases (
                        lane TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
                        acquired_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO lane_leases(lane, job_id, acquired_at, heartbeat_at) VALUES (?, ?, ?, ?)",
                    leases,
                )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        failures = list(self.connection.execute("PRAGMA foreign_key_check"))
        if failures:
            raise RuntimeError("database migration produced invalid foreign-key references")

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

    def set_product_stage(
        self,
        number: int,
        stage: str,
        *,
        rank: int | None = None,
        reason: str = "",
    ) -> None:
        if stage not in PRODUCT_STAGES:
            raise ValueError(f"unknown product stage: {stage}")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT product_stage, project_rank, carryover_replan "
                "FROM issues WHERE number = ?",
                (number,),
            ).fetchone()
            if row is None:
                raise KeyError(f"issue #{number} not found")
            next_rank = row["project_rank"] if rank is None else int(rank)
            next_carryover = int(row["carryover_replan"]) if stage == "inbox" else 0
            if (
                row["product_stage"] == stage
                and row["project_rank"] == next_rank
                and int(row["carryover_replan"]) == next_carryover
            ):
                return
            conn.execute(
                "UPDATE issues SET product_stage=?, project_rank=?, carryover_replan=?, "
                "updated_at=? WHERE number=?",
                (stage, next_rank, next_carryover, utcnow(), number),
            )
            self.event(
                "product.transition",
                {
                    "from": row["product_stage"],
                    "to": stage,
                    "rank": next_rank,
                    **({"reason": reason} if reason else {}),
                },
                issue_number=number,
                connection=conn,
            )

    def active_sprint(self) -> dict[str, Any] | None:
        return self._dict(
            self.connection.execute(
                "SELECT * FROM sprints WHERE state='active' ORDER BY number DESC LIMIT 1"
            ).fetchone()
        )

    def list_sprints(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute("SELECT * FROM sprints ORDER BY number DESC")
        ]

    def sprint_items(self, sprint_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT si.*, i.title, i.url, i.controller_state, i.product_stage,
                       i.github_state, i.current_plan_hash
                FROM sprint_items si JOIN issues i ON i.number=si.issue_number
                WHERE si.sprint_id=? ORDER BY si.rank, si.issue_number
                """,
                (sprint_id,),
            )
        ]

    def current_sprint_item(self, issue_number: int) -> dict[str, Any] | None:
        return self._dict(
            self.connection.execute(
                """
                SELECT si.*, s.number AS sprint_number, s.title AS sprint_title,
                       s.state AS sprint_state, s.iteration_id, s.starts_at, s.ends_at
                FROM sprint_items si JOIN sprints s ON s.id=si.sprint_id
                WHERE si.issue_number=? AND s.state='active'
                ORDER BY s.number DESC LIMIT 1
                """,
                (issue_number,),
            ).fetchone()
        )

    def latest_sprint_item(self, issue_number: int) -> dict[str, Any] | None:
        return self._dict(
            self.connection.execute(
                """
                SELECT si.*, s.number AS sprint_number, s.title AS sprint_title,
                       s.state AS sprint_state, s.iteration_id, s.starts_at, s.ends_at,
                       s.closed_at
                FROM sprint_items si JOIN sprints s ON s.id=si.sprint_id
                WHERE si.issue_number=? ORDER BY s.number DESC LIMIT 1
                """,
                (issue_number,),
            ).fetchone()
        )

    def next_sprint_number(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(number), -1) + 1 AS number FROM sprints"
        ).fetchone()
        return int(row["number"] if row else 0)

    def start_sprint(
        self,
        *,
        title: str,
        duration_days: int,
        starts_at: str,
        ends_at: str,
        iteration_id: str | None,
        issue_numbers: list[int],
    ) -> dict[str, Any]:
        if not issue_numbers:
            raise ValueError("cannot start an empty sprint")
        if len(issue_numbers) != len(set(issue_numbers)):
            raise ValueError("sprint issue list contains duplicates")
        now = utcnow()
        with self.transaction() as conn:
            active = conn.execute("SELECT number FROM sprints WHERE state='active'").fetchone()
            if active is not None:
                raise RuntimeError(f"Sprint {active['number']} is already active")
            number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(number), -1) + 1 AS number FROM sprints"
                ).fetchone()["number"]
            )
            cursor = conn.execute(
                """
                INSERT INTO sprints(number,title,state,duration_days,iteration_id,
                                    starts_at,ends_at,created_at)
                VALUES (?,?,'active',?,?,?,?,?)
                """,
                (number, title, duration_days, iteration_id, starts_at, ends_at, now),
            )
            sprint_id = int(cursor.lastrowid)
            for rank, issue_number in enumerate(issue_numbers):
                issue = conn.execute(
                    "SELECT controller_state FROM issues WHERE number=?", (issue_number,)
                ).fetchone()
                if issue is None:
                    raise KeyError(f"issue #{issue_number} not found")
                current = conn.execute(
                    """
                    SELECT p.plan_hash, p.plan_json, p.status FROM plans p JOIN issues i
                    ON i.current_plan_hash=p.plan_hash WHERE i.number=?
                    """,
                    (issue_number,),
                ).fetchone()
                plan = json.loads(current["plan_json"]) if current else {}
                conn.execute(
                    """
                    INSERT INTO sprint_items(sprint_id,issue_number,rank,commitment,status,
                                             story_points,points_evidence,added_at)
                    VALUES (?,?,?,'committed','active',?,?,?)
                    """,
                    (
                        sprint_id,
                        issue_number,
                        rank,
                        plan.get("story_points"),
                        plan.get("points_evidence"),
                        now,
                    ),
                )
                job_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM jobs WHERE issue_number=?",
                        (issue_number,),
                    ).fetchone()["count"]
                )
                retire_unestimated = bool(
                    current and plan.get("story_points") is None and job_count == 0
                )
                if current and (
                    current["status"] in {"rejected", "needs_split"} or retire_unestimated
                ):
                    conn.execute(
                        "UPDATE plans SET status='superseded', superseded_at=? WHERE plan_hash=?",
                        (now, current["plan_hash"]),
                    )
                    conn.execute(
                        "UPDATE approvals SET revoked_at=? WHERE plan_hash=?",
                        (now, current["plan_hash"]),
                    )
                    conn.execute(
                        "UPDATE issues SET current_plan_hash=NULL WHERE number=?", (issue_number,)
                    )
                    conn.execute(
                        "UPDATE issues SET controller_state='planning' WHERE number=?",
                        (issue_number,),
                    )
                conn.execute(
                    """
                    UPDATE issues SET product_stage='active', project_rank=?, carryover_replan=0,
                                      controller_state=CASE
                                          WHEN controller_state IN ('observed','blocked') THEN 'planning'
                                          ELSE controller_state
                                      END,
                                      updated_at=? WHERE number=?
                    """,
                    (rank, now, issue_number),
                )
                self.event(
                    "sprint.item_added",
                    {"sprint": number, "rank": rank, "commitment": "committed"},
                    issue_number=issue_number,
                    connection=conn,
                )
            self.event(
                "sprint.started",
                {
                    "sprint": number,
                    "title": title,
                    "duration_days": duration_days,
                    "issues": issue_numbers,
                },
                connection=conn,
            )
        return self.active_sprint() or {}

    def add_to_active_sprint(self, issue_number: int) -> dict[str, Any]:
        now = utcnow()
        with self.transaction() as conn:
            sprint = conn.execute(
                "SELECT * FROM sprints WHERE state='active' ORDER BY number DESC LIMIT 1"
            ).fetchone()
            if sprint is None:
                raise RuntimeError("there is no active sprint")
            existing = conn.execute(
                "SELECT * FROM sprint_items WHERE sprint_id=? AND issue_number=?",
                (sprint["id"], issue_number),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            issue = conn.execute("SELECT * FROM issues WHERE number=?", (issue_number,)).fetchone()
            if issue is None:
                raise KeyError(f"issue #{issue_number} not found")
            rank = int(
                conn.execute(
                    "SELECT COALESCE(MAX(rank), -1) + 1 AS rank FROM sprint_items WHERE sprint_id=?",
                    (sprint["id"],),
                ).fetchone()["rank"]
            )
            current = conn.execute(
                """
                SELECT p.plan_hash, p.plan_json, p.status FROM plans p JOIN issues i
                ON i.current_plan_hash=p.plan_hash WHERE i.number=?
                """,
                (issue_number,),
            ).fetchone()
            plan = json.loads(current["plan_json"]) if current else {}
            conn.execute(
                """
                INSERT INTO sprint_items(sprint_id,issue_number,rank,commitment,status,
                                         story_points,points_evidence,added_at)
                VALUES (?,?,?,'added','active',?,?,?)
                """,
                (
                    sprint["id"],
                    issue_number,
                    rank,
                    plan.get("story_points"),
                    plan.get("points_evidence"),
                    now,
                ),
            )
            job_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM jobs WHERE issue_number=?",
                    (issue_number,),
                ).fetchone()["count"]
            )
            retire_unestimated = bool(
                current and plan.get("story_points") is None and job_count == 0
            )
            if current and (
                current["status"] in {"rejected", "needs_split"} or retire_unestimated
            ):
                conn.execute(
                    "UPDATE plans SET status='superseded', superseded_at=? WHERE plan_hash=?",
                    (now, current["plan_hash"]),
                )
                conn.execute(
                    "UPDATE approvals SET revoked_at=? WHERE plan_hash=?",
                    (now, current["plan_hash"]),
                )
                conn.execute("UPDATE issues SET current_plan_hash=NULL WHERE number=?", (issue_number,))
                conn.execute(
                    "UPDATE issues SET controller_state='planning' WHERE number=?",
                    (issue_number,),
                )
            conn.execute(
                """
                UPDATE issues SET product_stage='active', project_rank=?, carryover_replan=0,
                                  controller_state=CASE
                                      WHEN controller_state IN ('observed','blocked') THEN 'planning'
                                      ELSE controller_state
                                  END,
                                  updated_at=? WHERE number=?
                """,
                (rank, now, issue_number),
            )
            self.event(
                "sprint.item_added",
                {"sprint": sprint["number"], "rank": rank, "commitment": "added"},
                issue_number=issue_number,
                connection=conn,
            )
        return self.current_sprint_item(issue_number) or {}

    def set_sprint_iteration(self, sprint_id: int, iteration_id: str) -> None:
        self.connection.execute(
            "UPDATE sprints SET iteration_id=? WHERE id=?", (iteration_id, sprint_id)
        )

    def record_story_points(
        self,
        issue_number: int,
        points: int,
        evidence: str,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT si.* FROM sprint_items si JOIN sprints s ON s.id=si.sprint_id
                WHERE si.issue_number=? AND s.state='active'
                """,
                (issue_number,),
            ).fetchone()
            if row is None:
                return
            if row["story_points"] == points and row["points_evidence"] == evidence:
                return
            conn.execute(
                "UPDATE sprint_items SET story_points=?, points_evidence=? WHERE sprint_id=? AND issue_number=?",
                (points, evidence, row["sprint_id"], issue_number),
            )
            self.event(
                "sprint.points_recorded",
                {"points": points, "evidence": evidence},
                issue_number=issue_number,
                connection=conn,
            )

    def set_sprint_item_status(self, issue_number: int, status: str) -> None:
        if status not in SPRINT_ITEM_STATES:
            raise ValueError(f"unknown sprint item status: {status}")
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT si.* FROM sprint_items si JOIN sprints s ON s.id=si.sprint_id
                WHERE si.issue_number=? AND s.state='active'
                """,
                (issue_number,),
            ).fetchone()
            if row is None or row["status"] == status:
                return
            conn.execute(
                "UPDATE sprint_items SET status=?, completed_at=? WHERE sprint_id=? AND issue_number=?",
                (
                    status,
                    utcnow() if status != "active" else None,
                    row["sprint_id"],
                    issue_number,
                ),
            )
            self.event(
                "sprint.item_status",
                {"from": row["status"], "to": status},
                issue_number=issue_number,
                connection=conn,
            )

    def close_active_sprint(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        now = utcnow()
        with self.transaction() as conn:
            sprint = conn.execute(
                "SELECT * FROM sprints WHERE state='active' ORDER BY number DESC LIMIT 1"
            ).fetchone()
            if sprint is None:
                raise RuntimeError("there is no active sprint")
            items = list(
                conn.execute(
                    "SELECT * FROM sprint_items WHERE sprint_id=? ORDER BY rank, issue_number",
                    (sprint["id"],),
                )
            )
            if not items or any(item["status"] == "active" for item in items):
                raise RuntimeError("active sprint still contains non-terminal work")
            conn.execute(
                "UPDATE sprints SET state='closed', closed_at=? WHERE id=?",
                (now, sprint["id"]),
            )
            for item in items:
                stage = "done" if item["status"] == "done" else (
                    "needs_split" if item["status"] == "needs_split" else "inbox"
                )
                carryover_replan = int(item["status"] == "blocked")
                if carryover_replan:
                    current = conn.execute(
                        "SELECT current_plan_hash FROM issues WHERE number=?",
                        (item["issue_number"],),
                    ).fetchone()
                    plan_hash = str(current["current_plan_hash"] or "") if current else ""
                    blockers: list[dict[str, Any]] = []
                    if plan_hash:
                        blockers = self._retire_plan_graph(
                            conn,
                            int(item["issue_number"]),
                            plan_hash,
                            reason=f"blocked carryover from Sprint {sprint['number']}",
                            now=now,
                        )
                    conn.execute(
                        "UPDATE issues SET current_plan_hash=NULL, controller_state='planning' "
                        "WHERE number=?",
                        (item["issue_number"],),
                    )
                    self.event(
                        "sprint.carryover_replan_requested",
                        {
                            "sprint": sprint["number"],
                            "plan_hash": plan_hash or None,
                            "blockers": blockers,
                        },
                        issue_number=item["issue_number"],
                        connection=conn,
                    )
                conn.execute(
                    "UPDATE issues SET product_stage=?, project_rank=?, carryover_replan=?, "
                    "updated_at=? WHERE number=?",
                    (
                        stage,
                        item["rank"],
                        carryover_replan,
                        now,
                        item["issue_number"],
                    ),
                )
            self.event(
                "sprint.closed",
                {
                    "sprint": sprint["number"],
                    "done": [item["issue_number"] for item in items if item["status"] == "done"],
                    "returned": [item["issue_number"] for item in items if item["status"] != "done"],
                },
                connection=conn,
            )
        closed = dict(sprint)
        closed.update({"state": "closed", "closed_at": now})
        return closed, [dict(item) for item in items]

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

    def mark_plan_needs_split(self, issue_number: int, plan_hash: str) -> bool:
        now = utcnow()
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT current_plan_hash FROM issues WHERE number=?", (issue_number,)
            ).fetchone()
            if current is None or current["current_plan_hash"] != plan_hash:
                return False
            conn.execute("UPDATE plans SET status='needs_split' WHERE plan_hash=?", (plan_hash,))
            conn.execute(
                "UPDATE issues SET controller_state='blocked', product_stage='needs_split', "
                "carryover_replan=0, updated_at=? WHERE number=?",
                (now, issue_number),
            )
            conn.execute(
                """
                UPDATE sprint_items SET status='needs_split', completed_at=?
                WHERE issue_number=? AND sprint_id=(SELECT id FROM sprints WHERE state='active')
                """,
                (now, issue_number),
            )
            self.event(
                "plan.needs_split",
                {"plan_hash": plan_hash, "story_points": 21},
                issue_number=issue_number,
                connection=conn,
            )
        return True

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

    def prepare_auto_replan(
        self,
        issue_number: int,
        plan_hash: str,
        detail: dict[str, Any],
    ) -> None:
        """Durably record external cleanup intent before any PR is closed."""
        latest = self.latest_event(issue_number, "plan.auto_replan_prepared")
        if latest and latest["detail"].get("plan_hash") == plan_hash:
            return
        self.event(
            "plan.auto_replan_prepared",
            {"plan_hash": plan_hash, **detail},
            issue_number=issue_number,
        )

    def prepare_sprint_carryover(
        self,
        issue_number: int,
        sprint_number: int,
        plan_hash: str | None,
        pull_requests: list[int],
    ) -> None:
        """Persist carryover cleanup intent before changing controller-owned PRs."""
        latest = self.latest_event(issue_number, "sprint.carryover_prepared")
        if latest and latest["detail"].get("sprint") == sprint_number:
            return
        self.event(
            "sprint.carryover_prepared",
            {
                "sprint": sprint_number,
                "plan_hash": plan_hash,
                "pull_requests": sorted(pull_requests),
            },
            issue_number=issue_number,
        )

    def supersede_plan_for_replan(
        self,
        issue_number: int,
        plan_hash: str,
        *,
        reason: str,
        blockers: list[dict[str, Any]],
    ) -> bool:
        """Atomically retire the approved execution graph and request a fresh plan."""
        now = utcnow()
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT current_plan_hash FROM issues WHERE number = ?",
                (issue_number,),
            ).fetchone()
            if current is None or current["current_plan_hash"] != plan_hash:
                return False
            self._retire_plan_graph(
                conn,
                issue_number,
                plan_hash,
                reason=reason,
                now=now,
            )
            conn.execute(
                "UPDATE issues SET current_plan_hash=NULL, controller_state='planning', updated_at=? WHERE number=?",
                (now, issue_number),
            )
            self.event(
                "plan.auto_replan_requested",
                {
                    "plan_hash": plan_hash,
                    "reason": reason,
                    "blockers": blockers,
                },
                issue_number=issue_number,
                connection=conn,
            )
        return True

    def _retire_plan_graph(
        self,
        conn: sqlite3.Connection,
        issue_number: int,
        plan_hash: str,
        *,
        reason: str,
        now: str,
    ) -> list[dict[str, Any]]:
        """Retire one execution graph while retaining merged work and blocker evidence."""
        jobs = list(
            conn.execute(
                "SELECT * FROM jobs WHERE issue_number=? AND plan_hash=? "
                "ORDER BY created_at, job_id",
                (issue_number, plan_hash),
            )
        )
        blockers: list[dict[str, Any]] = []
        for job in jobs:
            if job["state"] == "merged":
                continue
            blockers.append(
                {
                    "job_id": job["job_id"],
                    "lane": job["lane"],
                    "state": job["state"],
                    "pr_number": job["pr_number"],
                    "reason": job["last_error"] or reason,
                }
            )
            lease = conn.execute(
                "SELECT lane FROM lane_leases WHERE job_id=?", (job["job_id"],)
            ).fetchone()
            conn.execute("DELETE FROM lane_leases WHERE job_id=?", (job["job_id"],))
            if lease is not None:
                self.event(
                    "lane.released",
                    {"lane": lease["lane"], "reason": "job entered superseded"},
                    issue_number=issue_number,
                    job_id=job["job_id"],
                    connection=conn,
                )
            conn.execute(
                "UPDATE jobs SET state='superseded', last_error=?, updated_at=? WHERE job_id=?",
                (job["last_error"] or reason, now, job["job_id"]),
            )
            if job["state"] != "superseded":
                self.event(
                    "job.transition",
                    {"from": job["state"], "to": "superseded"},
                    issue_number=issue_number,
                    job_id=job["job_id"],
                    connection=conn,
                )
        conn.execute(
            "UPDATE plans SET status='superseded', superseded_at=? WHERE plan_hash=?",
            (now, plan_hash),
        )
        conn.execute("UPDATE approvals SET revoked_at=? WHERE plan_hash=?", (now, plan_hash))
        return blockers

    def clear_carryover_replan(self, issue_number: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE issues SET carryover_replan=0, updated_at=? WHERE number=?",
                (utcnow(), issue_number),
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
                "UPDATE issues SET controller_state='approved', carryover_replan=0, "
                "updated_at=? WHERE number=?",
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
                "UPDATE issues SET controller_state='blocked', carryover_replan=0, "
                "updated_at=? WHERE number=?",
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
                    INSERT INTO jobs(job_id, issue_number, plan_hash, lane, title, state, branch, base_sha,
                                     paths_json, verify_command, contracts_json, depends_on_json,
                                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO NOTHING
                    """,
                    (
                        job["id"],
                        issue_number,
                        plan_hash,
                        job["lane"],
                        job["title"],
                        job["branch"],
                        None if job.get("depends_on") else plan["base_sha"],
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
        return self.list_current_jobs(issue_number)

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

    def list_current_jobs(self, issue_number: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT j.* FROM jobs j
            JOIN issues i ON i.number = j.issue_number AND i.current_plan_hash = j.plan_hash
            WHERE j.issue_number = ?
            ORDER BY j.created_at, j.job_id
            """,
            (issue_number,),
        )
        return [self.decode_job(row) for row in rows]

    def plan_count(self, issue_number: int) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM plans WHERE issue_number = ?",
            (issue_number,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def event_count(self, issue_number: int, kind: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE issue_number = ? AND kind = ?",
            (issue_number, kind),
        ).fetchone()
        return int(row["count"] if row else 0)

    def auto_replan_attempt_count(self, issue_number: int) -> int:
        """Count consecutive automatic replans since the latest merged progress."""
        rows = self.connection.execute(
            """
            SELECT kind, detail_json FROM events
            WHERE issue_number = ?
              AND kind IN ('job.transition', 'plan.auto_replan_requested')
            ORDER BY id
            """,
            (issue_number,),
        )
        attempts = 0
        for row in rows:
            if row["kind"] == "plan.auto_replan_requested":
                attempts += 1
                continue
            try:
                detail = json.loads(row["detail_json"])
            except (TypeError, ValueError):
                continue
            if detail.get("to") == "merged":
                attempts = 0
        return attempts

    def update_job(self, job_id: str, *, state: str | None = None, **fields: Any) -> dict[str, Any]:
        if state is not None and state not in JOB_STATES:
            raise ValueError(f"unknown job state: {state}")
        allowed = {
            "workspace_id",
            "workspace_url",
            "agent_id",
            "base_sha",
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
            if "base_sha" in fields:
                candidate = fields["base_sha"]
                existing = previous["base_sha"]
                if not isinstance(candidate, str) or not candidate:
                    raise ValueError("job base_sha must be a non-empty string")
                if existing and existing != candidate:
                    raise ValueError(
                        f"job {job_id} base_sha is immutable ({existing[:12]} != {candidate[:12]})"
                    )
            updates = dict(fields)
            if state is not None:
                updates["state"] = state
            updates["updated_at"] = utcnow()
            assignments = ", ".join(f"{name} = ?" for name in updates)
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?",
                (*updates.values(), job_id),
            )
            if state is not None and state not in ACTIVE_LEASE_STATES:
                lease = conn.execute(
                    "SELECT lane FROM lane_leases WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                conn.execute("DELETE FROM lane_leases WHERE job_id = ?", (job_id,))
                if lease is not None:
                    self.event(
                        "lane.released",
                        {"lane": lease["lane"], "reason": f"job entered {state}"},
                        issue_number=previous["issue_number"],
                        job_id=job_id,
                        connection=conn,
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
            job = conn.execute(
                "SELECT lane FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"job {job_id} not found")
            if job["lane"] != lane:
                raise ValueError(
                    f"job {job_id} belongs to lane {job['lane']}, not {lane}"
                )
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

    def prune_lane_leases(self) -> list[dict[str, Any]]:
        """Remove durable ownership that no longer represents active lane work."""
        placeholders = ",".join("?" for _ in ACTIVE_LEASE_STATES)
        with self.transaction() as conn:
            rows = list(
                conn.execute(
                    f"""
                    SELECT l.lane, l.job_id, j.issue_number, j.state
                    FROM lane_leases l
                    LEFT JOIN jobs j ON j.job_id = l.job_id
                    WHERE j.job_id IS NULL OR j.state NOT IN ({placeholders})
                    ORDER BY l.lane
                    """,
                    tuple(sorted(ACTIVE_LEASE_STATES)),
                )
            )
            for row in rows:
                conn.execute("DELETE FROM lane_leases WHERE lane = ?", (row["lane"],))
                self.event(
                    "lane.lease_reconciled",
                    {
                        "lane": row["lane"],
                        "previous_state": row["state"],
                        "reason": "lease owner is not active",
                    },
                    issue_number=row["issue_number"],
                    job_id=row["job_id"],
                    connection=conn,
                )
        return [dict(row) for row in rows]

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

    def latest_event(self, issue_number: int, kind: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM events WHERE issue_number = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (issue_number, kind),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json"))
        return item

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
