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
            operating_brief TEXT DEFAULT '{}',
            invocation_count INTEGER DEFAULT 0,
            model TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    # Migrate existing DBs that lack the operating_brief column
    try:
        conn.execute("SELECT operating_brief FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN operating_brief TEXT DEFAULT '{}'")
        conn.commit()

    # Migrate existing DBs that lack the model column
    try:
        conn.execute("SELECT model FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN model TEXT DEFAULT NULL")
        conn.commit()

    # Migrate: cwd column (custom working directory)
    try:
        conn.execute("SELECT cwd FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN cwd TEXT DEFAULT NULL")
        conn.commit()

    # Migrate: channels column (additional dev channel servers)
    try:
        conn.execute("SELECT channels FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN channels TEXT DEFAULT '[]'")
        conn.commit()

    # Migrate: kind column (task or service)
    try:
        conn.execute("SELECT kind FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN kind TEXT DEFAULT 'task'")
        conn.commit()

    # Migrate: host column (which mesh host the task runs on; NULL = local)
    try:
        conn.execute("SELECT host FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN host TEXT DEFAULT NULL")
        conn.commit()

    # Migrate: last_seen_at — most recent liveness signal (heartbeat or hook fire).
    try:
        conn.execute("SELECT last_seen_at FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_seen_at TEXT DEFAULT NULL")
        conn.commit()

    # Migrate: last_error — short string describing the most recent failure.
    try:
        conn.execute("SELECT last_error FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_error TEXT DEFAULT NULL")
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
    channels: list[str] | None = None,
    kind: str = "task",
    host: str | None = None,
) -> dict:
    port = allocate_port(conn)
    conn.execute(
        """INSERT INTO tasks (task_id, name, description, port, plugins, operating_brief, model, cwd, channels, kind, host)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, name, description, port,
         json.dumps(plugins or []), json.dumps(operating_brief or {}), model,
         cwd, json.dumps(channels or []), kind, host),
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


def mark_seen(conn: sqlite3.Connection, task_id: str) -> None:
    """Stamp last_seen_at = now. Called whenever the agent emits a hook or the
    liveness reconciler confirms the tmux session is alive."""
    conn.execute(
        "UPDATE tasks SET last_seen_at = datetime('now') WHERE task_id = ?",
        (task_id,),
    )
    conn.commit()


def mark_crashed(conn: sqlite3.Connection, task_id: str, error: str) -> None:
    """Flip status to 'crashed' and record why. Called when the liveness
    reconciler finds a row marked 'running' but no live tmux session."""
    conn.execute(
        """UPDATE tasks SET status = 'crashed', last_error = ?, updated_at = datetime('now')
           WHERE task_id = ?""",
        (error, task_id),
    )
    conn.commit()


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Delete a task from the database. Returns True if a row was deleted."""
    cursor = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    conn.commit()
    return cursor.rowcount > 0
