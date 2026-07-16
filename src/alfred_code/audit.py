from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .util import utcnow


class AuditLog:
    """Append-only, fsynced JSONL evidence for human and machine inspection."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **detail: Any) -> None:
        record = json.dumps(
            {"at": utcnow(), "kind": kind, **detail},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ) + "\n"
        with self.path.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(record)
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
