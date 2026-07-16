from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .errors import CommandError


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def run(
    command: Iterable[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> str:
    argv = [str(part) for part in command]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            env=merged_env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(argv, 127, str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(argv, 124, f"timed out after {timeout}s") from exc
    if completed.returncode:
        raise CommandError(argv, completed.returncode, completed.stderr)
    return completed.stdout


def run_json(command: Iterable[str], **kwargs: Any) -> Any:
    output = run(command, **kwargs)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise CommandError([str(x) for x in command], 65, f"invalid JSON output: {exc}") from exc


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
