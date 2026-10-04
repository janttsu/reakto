"""Private verdict cache in ~/.local/state/reakto (never inside the mail directory)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    d = base / "reakto"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)  # verdicts quote private mail
    return d


def cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


class Cache:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "cache.sqlite"
        new = not self.path.exists()
        self.db = sqlite3.connect(self.path)
        if new:
            os.chmod(self.path, 0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS verdicts (key TEXT PRIMARY KEY, sha256 TEXT, stage TEXT,"
            " data TEXT, created REAL)"
        )
        self.db.commit()

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT data FROM verdicts WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, sha256: str, stage: str, data: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO verdicts VALUES (?, ?, ?, ?, ?)",
            (key, sha256, stage, json.dumps(data, ensure_ascii=False), time.time()),
        )
        self.db.commit()

    def models(self) -> list[str]:
        """Models that have verdicts in the cache, most used first."""
        rows = self.db.execute(
            "SELECT json_extract(data, '$.model') AS m, COUNT(*) FROM verdicts"
            " WHERE m IS NOT NULL AND m != '' GROUP BY m ORDER BY 2 DESC"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self.db.close()
