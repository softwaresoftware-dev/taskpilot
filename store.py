"""SQLite storage layer for taskpilot."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_data_dir = Path(os.environ.get("TASKPILOT_DATA_DIR", str(Path.home() / ".taskpilot")))
DEFAULT_DB_PATH = _data_dir / "taskpilot.db"
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


@contextmanager
def db(db_path: str | None = None):
    """Open a connection and guarantee it closes — even if the caller raises.

        with store.db() as conn:
            task = store.get_task(conn, task_id)
    """
    conn = get_db(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            port INTEGER UNIQUE,
            plugins TEXT DEFAULT '[]',
            operating_brief TEXT DEFAULT '{}',
            invocation_count INTEGER DEFAULT 0,
            model TEXT DEFAULT NULL,
            cwd TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # Lightweight migrations for DBs created by an earlier schema. Columns the
    # pared-down build no longer uses (kind, host, channels, session_id,
    # last_seen_at, last_error) are simply left in place if present — extra
    # columns are harmless. We only ADD columns the current code reads.
    for col, ddl in (
        ("operating_brief", "ALTER TABLE tasks ADD COLUMN operating_brief TEXT DEFAULT '{}'"),
        ("model", "ALTER TABLE tasks ADD COLUMN model TEXT DEFAULT NULL"),
        ("cwd", "ALTER TABLE tasks ADD COLUMN cwd TEXT DEFAULT NULL"),
    ):
        try:
            conn.execute(f"SELECT {col} FROM tasks LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(ddl)
            conn.commit()

    # Status vocabulary migration (0.15.0): defined → running → crashed /
    # stopped / completed. Legacy rows used 'pending' and 'killed'. Idempotent
    # and cheap, so it runs on every open.
    conn.execute("UPDATE tasks SET status = 'defined' WHERE status = 'pending'")
    conn.execute("UPDATE tasks SET status = 'stopped' WHERE status = 'killed'")
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
    operating_brief: dict | None = None,
    model: str | None = None,
    cwd: str | None = None,
) -> dict:
    port = allocate_port(conn)
    conn.execute(
        """INSERT INTO tasks (task_id, name, description, status, port, plugins, operating_brief, model, cwd)
           VALUES (?, ?, ?, 'defined', ?, ?, ?, ?, ?)""",
        (task_id, name, description, port,
         json.dumps(plugins or []), json.dumps(operating_brief or {}), model, cwd),
    )
    conn.commit()
    return get_task(conn, task_id)


def update_definition(
    conn: sqlite3.Connection,
    task_id: str,
    name: str,
    description: str,
    plugins: list[str] | None = None,
    operating_brief: dict | None = None,
    model: str | None = None,
    cwd: str | None = None,
) -> None:
    """Replace a task's definition in place (PUT semantics). Status and
    invocation history are untouched."""
    conn.execute(
        """UPDATE tasks SET name = ?, description = ?, plugins = ?,
           operating_brief = ?, model = ?, cwd = ?, updated_at = datetime('now')
           WHERE task_id = ?""",
        (name, description, json.dumps(plugins or []),
         json.dumps(operating_brief or {}), model, cwd, task_id),
    )
    conn.commit()


def delete_task(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete a task row, freeing its id for reuse."""
    conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    conn.commit()


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
