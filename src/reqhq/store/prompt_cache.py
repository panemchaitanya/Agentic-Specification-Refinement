"""SQLite-backed SHA-256 prompt response cache."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PromptCache:
    """Deterministic SHA-256 prompt cache persisted in SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS prompt_cache (
                        cache_key TEXT PRIMARY KEY,
                        model TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        hit_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cache_metrics (
                        metric_name TEXT PRIMARY KEY,
                        metric_value INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO cache_metrics (metric_name, metric_value) VALUES ('hits', 0)"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO cache_metrics (metric_name, metric_value) VALUES ('misses', 0)"
                )

    @staticmethod
    def compute_key(
        prompt: str,
        model: str,
        schema_name: str | None = None,
        system_prompt: str = "",
    ) -> str:
        """Compute deterministic SHA-256 cache key across prompt, model, schema, and system prompt."""
        canonical = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "schema_name": schema_name,
                "system_prompt": system_prompt,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        """Fetch cached response payload by key, updating hit/miss statistics."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                row = conn.execute(
                    "SELECT response_json FROM prompt_cache WHERE cache_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "UPDATE cache_metrics SET metric_value = metric_value + 1 WHERE metric_name = 'misses'"
                    )
                    return None

                conn.execute(
                    "UPDATE prompt_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
                    (key,),
                )
                conn.execute(
                    "UPDATE cache_metrics SET metric_value = metric_value + 1 WHERE metric_name = 'hits'"
                )
                return json.loads(row[0])

    def put(
        self,
        key: str,
        response_payload: dict[str, Any],
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or replace a cached response payload."""
        response_json = json.dumps(response_payload, sort_keys=True, ensure_ascii=False)
        metadata_json = json.dumps(dict(metadata or {}), sort_keys=True, ensure_ascii=False)
        created_at = _utc_now_iso()

        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO prompt_cache (cache_key, model, response_json, metadata_json, created_at, hit_count)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        model = excluded.model,
                        response_json = excluded.response_json,
                        metadata_json = excluded.metadata_json,
                        created_at = excluded.created_at
                    """,
                    (key, model, response_json, metadata_json, created_at),
                )

    def stats(self) -> dict[str, int]:
        """Return entry count, cumulative hits, and cumulative misses."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            entries_row = conn.execute("SELECT COUNT(*) FROM prompt_cache").fetchone()
            metrics_rows = conn.execute(
                "SELECT metric_name, metric_value FROM cache_metrics"
            ).fetchall()

        metrics = {name: int(val) for name, val in metrics_rows}
        return {
            "entries": int(entries_row[0]) if entries_row else 0,
            "hits": metrics.get("hits", 0),
            "misses": metrics.get("misses", 0),
        }

    def clear(self) -> int:
        """Remove all cached entries and return how many rows were deleted."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                cursor = conn.execute("DELETE FROM prompt_cache")
                return int(cursor.rowcount)


__all__ = ["PromptCache"]
