"""
db/state_manager.py
───────────────────
SQLite-backed persistence for scan history and deletion audit log.
Enables crash recovery, retry of failed deletions, and full audit trail.
"""

from __future__ import annotations

import sqlite3
import os
from datetime import datetime
from typing import Optional

from models.resource import CloudResource, DeletionStatus

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "cleanup_state.db")


class StateManager:
    """Thread-safe SQLite wrapper.  One instance per application lifetime."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = os.path.abspath(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── Connection ────────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS deletion_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id     TEXT    NOT NULL,
                resource_name   TEXT,
                resource_type   TEXT,
                provider        TEXT    NOT NULL,
                region          TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'PENDING',
                error_message   TEXT,
                snapshot_id     TEXT,
                attempted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at    TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scan_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                provider        TEXT    NOT NULL,
                region          TEXT    NOT NULL,
                mode            TEXT    NOT NULL,
                resources_found INTEGER DEFAULT 0,
                scanned_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

    # ── Deletion Log ──────────────────────────────────────────────────────────

    def log_attempt(self, resource: CloudResource) -> int:
        """Insert a PENDING record; returns the new row id."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO deletion_log
               (resource_id, resource_name, resource_type, provider, region, status)
               VALUES (?, ?, ?, ?, ?, 'PENDING')""",
            (resource.resource_id, resource.name,
             resource.resource_type.value,
             resource.provider.value, resource.region),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def mark_success(self, resource_id: str, snapshot_id: Optional[str] = None) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE deletion_log
               SET status='SUCCESS', completed_at=?, snapshot_id=?
               WHERE resource_id=? AND status='PENDING'""",
            (datetime.utcnow().isoformat(), snapshot_id, resource_id),
        )
        conn.commit()

    def mark_failed(self, resource_id: str, error: str) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE deletion_log
               SET status='FAILED', completed_at=?, error_message=?
               WHERE resource_id=? AND status='PENDING'""",
            (datetime.utcnow().isoformat(), error, resource_id),
        )
        conn.commit()

    def mark_blocked(self, resource_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE deletion_log
               SET status='BLOCKED', completed_at=?
               WHERE resource_id=? AND status='PENDING'""",
            (datetime.utcnow().isoformat(), resource_id),
        )
        conn.commit()

    def get_failed(self, provider: Optional[str] = None) -> list[dict]:
        conn = self._get_conn()
        if provider:
            rows = conn.execute(
                """SELECT * FROM deletion_log
                   WHERE status='FAILED' AND provider=?
                   AND resource_id NOT IN (
                       SELECT resource_id FROM deletion_log WHERE status='SUCCESS'
                   )""",
                (provider,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM deletion_log
                   WHERE status='FAILED'
                   AND resource_id NOT IN (
                       SELECT resource_id FROM deletion_log WHERE status='SUCCESS'
                   )""",
            ).fetchall()
        return [dict(r) for r in rows]

    def get_pending(self) -> list[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM deletion_log WHERE status='PENDING'"
        ).fetchall()
        return [dict(r) for r in rows]

    def already_deleted(self, resource_id: str) -> bool:
        row = self._get_conn().execute(
            "SELECT 1 FROM deletion_log WHERE resource_id=? AND status='SUCCESS'",
            (resource_id,),
        ).fetchone()
        return row is not None

    def get_history(self, limit: int = 200) -> list[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM deletion_log ORDER BY attempted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Scan History ─────────────────────────────────────────────────────────

    def record_scan(self, provider: str, region: str, mode: str, count: int) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO scan_history (provider, region, mode, resources_found)
               VALUES (?, ?, ?, ?)""",
            (provider, region, mode, count),
        )
        conn.commit()

    def get_last_scan_count(self, provider: str, region: str, mode: str) -> int:
        row = self._get_conn().execute(
            """SELECT resources_found FROM scan_history
               WHERE provider=? AND region=? AND mode=?
               ORDER BY scanned_at DESC LIMIT 1""",
            (provider, region, mode),
        ).fetchone()
        return row["resources_found"] if row else 0

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
