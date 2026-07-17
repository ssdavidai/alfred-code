from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .agent_security import (
    SCOPED_CLAUDE_AGENT_ID,
    SCOPED_CODEX_AGENT_ID,
    SECURITY_POLICY,
    _codex_legacy_sandbox_conflict,
)
from .errors import ConfigurationError


def discover_host_database() -> Path:
    override = os.environ.get("SUPERSET_HOST_DB", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_file():
            return path
        raise ConfigurationError(f"SUPERSET_HOST_DB does not exist: {path}")
    candidates = sorted((Path.home() / ".superset/host").glob("*/host.db"))
    if len(candidates) != 1:
        raise ConfigurationError(
            f"expected exactly one local Superset host database, found {len(candidates)}"
        )
    return candidates[0]


def expected_agent_configs() -> list[dict[str, Any]]:
    command = str(Path.home() / ".claude/bin/alfred-code-agent")
    environment = json.dumps({"ALFRED_CODE_SECURITY_POLICY": SECURITY_POLICY}, separators=(",", ":"))
    return [
        {
            "id": SCOPED_CLAUDE_AGENT_ID,
            "preset_id": "claude",
            "label": "Alfred Claude (Scoped)",
            "command": command,
            "args_json": '["claude"]',
            "prompt_transport": "argv",
            "prompt_args_json": "[]",
            "env_json": environment,
            "display_order": 100,
        },
        {
            "id": SCOPED_CODEX_AGENT_ID,
            "preset_id": "codex",
            "label": "Alfred Codex (Scoped)",
            "command": command,
            "args_json": '["codex"]',
            "prompt_transport": "argv",
            "prompt_args_json": '["--"]',
            "env_json": environment,
            "display_order": 101,
        },
    ]


def hardened_builtin_arguments() -> dict[str, str]:
    claude_settings = {
        "permissions": {"disableBypassPermissionsMode": "disable"},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
        },
    }
    return {
        "claude": json.dumps(
            ["--permission-mode", "auto", "--settings", json.dumps(claude_settings, separators=(",", ":"))],
            separators=(",", ":"),
        ),
        "codex": '["--sandbox","workspace-write","--ask-for-approval","on-request"]',
    }


def provision_agent_configs(database: Path | None = None) -> dict[str, Any]:
    path = database or discover_host_database()
    now = int(time.time() * 1000)
    connection = sqlite3.connect(path, timeout=10)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(host_agent_configs)").fetchall()
        }
        required = {
            "id",
            "preset_id",
            "label",
            "command",
            "args_json",
            "prompt_transport",
            "prompt_args_json",
            "env_json",
            "display_order",
            "created_at",
            "updated_at",
        }
        if not required.issubset(columns):
            raise ConfigurationError("Superset host_agent_configs schema is not compatible")
        with connection:
            for preset_id, arguments in hardened_builtin_arguments().items():
                connection.execute(
                    """UPDATE host_agent_configs SET args_json = ?, updated_at = ?
                       WHERE preset_id = ? AND id NOT IN (?, ?)""",
                    (arguments, now, preset_id, SCOPED_CLAUDE_AGENT_ID, SCOPED_CODEX_AGENT_ID),
                )
            for item in expected_agent_configs():
                connection.execute(
                    """
                    INSERT INTO host_agent_configs(
                        id, preset_id, label, command, args_json, prompt_transport,
                        prompt_args_json, env_json, display_order, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        preset_id=excluded.preset_id,
                        label=excluded.label,
                        command=excluded.command,
                        args_json=excluded.args_json,
                        prompt_transport=excluded.prompt_transport,
                        prompt_args_json=excluded.prompt_args_json,
                        env_json=excluded.env_json,
                        display_order=excluded.display_order,
                        updated_at=excluded.updated_at
                    """,
                    (
                        item["id"],
                        item["preset_id"],
                        item["label"],
                        item["command"],
                        item["args_json"],
                        item["prompt_transport"],
                        item["prompt_args_json"],
                        item["env_json"],
                        item["display_order"],
                        now,
                        now,
                    ),
                )
    finally:
        connection.close()
    return inspect_agent_configs(path)


def inspect_agent_configs(database: Path | None = None) -> dict[str, Any]:
    path = database or discover_host_database()
    expected = {item["id"]: item for item in expected_agent_configs()}
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in expected)
        rows = connection.execute(
            f"SELECT * FROM host_agent_configs WHERE id IN ({placeholders})",
            tuple(expected),
        ).fetchall()
    finally:
        connection.close()
    actual = {str(row["id"]): dict(row) for row in rows}
    problems: list[str] = []
    for identifier, wanted in expected.items():
        row = actual.get(identifier)
        if row is None:
            problems.append(f"missing scoped Superset agent {identifier}")
            continue
        for key in (
            "preset_id",
            "label",
            "command",
            "args_json",
            "prompt_transport",
            "prompt_args_json",
            "env_json",
        ):
            if row.get(key) != wanted[key]:
                problems.append(f"{wanted['label']} has drifted field {key}")
        serialized = " ".join(str(row.get(key) or "") for key in ("command", "args_json", "env_json")).lower()
        if "dangerously" in serialized or '"bypasspermissions"' in serialized or "--yolo" in serialized:
            problems.append(f"{wanted['label']} contains a prohibited bypass setting")
    wrapper = Path.home() / ".claude/bin/alfred-code-agent"
    guard = Path.home() / ".claude/bin/alfred-code-agent-guard"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        problems.append(f"scoped agent launcher is not executable: {wrapper}")
    if not guard.is_file() or not os.access(guard, os.X_OK):
        problems.append(f"scoped agent guard is not executable: {guard}")
    if wrapper.is_file() and os.access(wrapper, os.X_OK):
        providers = sorted({str(value["preset_id"]) for value in expected.values()})
        for provider in providers:
            try:
                checked = subprocess.run(
                    [str(wrapper), "--self-check", provider],
                    text=True,
                    capture_output=True,
                    timeout=20,
                )
                self_check = json.loads(checked.stdout) if checked.returncode == 0 else {}
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                problems.append(f"scoped {provider} launcher self-check failed: {exc}")
                continue
            python_version = self_check.get("python") if isinstance(self_check, dict) else None
            if (
                checked.returncode != 0
                or not isinstance(self_check, dict)
                or self_check.get("policy") != SECURITY_POLICY
                or self_check.get("provider") != provider
                or not self_check.get("provider_version")
                or not isinstance(python_version, list)
                or tuple(python_version[:2]) < (3, 11)
            ):
                detail = (checked.stderr or checked.stdout or "invalid self-check output").strip()[:300]
                problems.append(f"scoped {provider} launcher self-check failed: {detail}")
    if guard.is_file() and os.access(guard, os.X_OK):
        try:
            guarded = subprocess.run(
                [str(guard)],
                input="{}",
                text=True,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"scoped agent guard self-check failed: {exc}")
        else:
            if guarded.returncode != 0:
                detail = (guarded.stderr or guarded.stdout or "guard exited non-zero").strip()[:300]
                problems.append(f"scoped agent guard self-check failed: {detail}")
    codex_conflict = _codex_legacy_sandbox_conflict(Path.home() / ".codex/config.toml")
    if codex_conflict:
        problems.append(codex_conflict)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        builtin_rows = connection.execute(
            """SELECT preset_id, label, command, args_json FROM host_agent_configs
               WHERE preset_id IN ('claude', 'codex') AND id NOT IN (?, ?)""",
            (SCOPED_CLAUDE_AGENT_ID, SCOPED_CODEX_AGENT_ID),
        ).fetchall()
    finally:
        connection.close()
    expected_builtin = hardened_builtin_arguments()
    for row in builtin_rows:
        preset_id = str(row["preset_id"])
        serialized = f"{row['command']} {row['args_json']}".lower()
        if "dangerously" in serialized or '"bypasspermissions"' in serialized or "--yolo" in serialized:
            problems.append(f"built-in Superset preset {row['label']} still contains a bypass setting")
        if row["args_json"] != expected_builtin[preset_id]:
            problems.append(f"built-in Superset preset {row['label']} has drifted from its hardened fallback")
    if problems:
        raise ConfigurationError("; ".join(problems))
    return {
        "database": str(path),
        "policy": SECURITY_POLICY,
        "agents": [
            {"id": identifier, "label": actual[identifier]["label"], "command": actual[identifier]["command"]}
            for identifier in expected
        ],
    }
