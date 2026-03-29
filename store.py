"""SQLite storage layer for taskpilot."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".taskpilot" / "taskpilot.db"
PORT_RANGE_START = 9100


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            port INTEGER UNIQUE,
            plugins TEXT DEFAULT '[]',
            invocation_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def allocate_port(conn: sqlite3.Connection) -> int:
    """Find the next available port starting from PORT_RANGE_START."""
    row = conn.execute(
        "SELECT MAX(port) as max_port FROM tasks"
    ).fetchone()
    max_port = row["max_port"] if row["max_port"] else PORT_RANGE_START - 1
    return max(max_port + 1, PORT_RANGE_START)


def create_task(
    conn: sqlite3.Connection,
    task_id: str,
    name: str,
    description: str,
    plugins: list[str] | None = None,
) -> dict:
    port = allocate_port(conn)
    conn.execute(
        """INSERT INTO tasks (task_id, name, description, port, plugins)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, name, description, port, json.dumps(plugins or [])),
    )
    conn.commit()
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if not row:
        return None
    return dict(row)


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE task_id = ?",
        (status, task_id),
    )
    conn.commit()


def increment_invocation(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET invocation_count = invocation_count + 1, updated_at = datetime('now') WHERE task_id = ?",
        (task_id,),
    )
    conn.commit()
