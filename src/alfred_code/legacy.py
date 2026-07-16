from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database
from .util import content_hash, utcnow


class LegacyImporter:
    """Imports evidence only. It never trusts legacy lifecycle fields as current truth."""

    def __init__(self, database: Database, legacy_dir: Path):
        self.database = database
        self.legacy_dir = legacy_dir

    def import_file(self, path: Path) -> dict[str, Any]:
        raw = path.read_text()
        digest = content_hash(raw)
        existing = self.database.connection.execute(
            "SELECT 1 FROM legacy_imports WHERE source_path=? AND content_hash=?",
            (str(path), digest),
        ).fetchone()
        if existing:
            return {"path": str(path), "status": "unchanged", "hash": digest}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            detail = {"status": "invalid", "error": str(exc)}
        else:
            count = len(value) if isinstance(value, (list, dict)) else 1
            detail = {"status": "observed", "type": type(value).__name__, "entries": count}
            self.database.observe("legacy", str(path), value)
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO legacy_imports(source_path, content_hash, imported_at, detail_json) VALUES (?, ?, ?, ?)",
                (str(path), digest, utcnow(), json.dumps(detail, sort_keys=True)),
            )
            self.database.event("legacy.imported", {"path": str(path), **detail}, connection=conn)
        return {"path": str(path), "hash": digest, **detail}

    def run(self) -> list[dict[str, Any]]:
        results = []
        for name in ("dispatched.json", "pending-gates.json", "gh-truth.json"):
            path = self.legacy_dir / name
            if path.exists():
                results.append(self.import_file(path))
        runs = self.legacy_dir / "runs"
        if runs.exists():
            logs = sorted(runs.glob("*.log"))
            summary = {
                "count": len(logs),
                "bytes": sum(path.stat().st_size for path in logs),
                "paths": [str(path) for path in logs],
            }
            self.database.observe("legacy", str(runs), summary)
            results.append({"path": str(runs), "status": "observed", **summary})
        return results
