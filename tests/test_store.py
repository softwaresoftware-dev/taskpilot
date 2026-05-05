"""Tests for the SQLite storage layer."""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
import store


@pytest.fixture
def db(tmp_path):
    """Create a fresh test database."""
    db_path = str(tmp_path / "test.db")
    conn = store.get_db(db_path)
    yield conn
    conn.close()


class TestSchema:
    def test_creates_tasks_table(self, db):
        cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        assert "tasks" in tables

    def test_operating_brief_column_exists(self, db):
        cursor = db.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        assert "operating_brief" in columns

    def test_model_column_exists(self, db):
        cursor = db.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        assert "model" in columns

    def test_migration_adds_column_to_old_schema(self, tmp_path):
        """Simulate an old DB without operating_brief and verify migration."""
        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE tasks (
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
        conn.close()

        # Now open with store.get_db which should migrate
        conn = store.get_db(db_path)
        cursor = conn.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        assert "operating_brief" in columns
        conn.close()


class TestCreateTask:
    def test_basic_create(self, db):
        task = store.create_task(db, "test-task", "Test", "Do something")
        assert task["task_id"] == "test-task"
        assert task["name"] == "Test"
        assert task["status"] == "pending"
        assert task["port"] >= store.PORT_RANGE_START

    def test_create_with_plugins(self, db):
        task = store.create_task(db, "t1", "T1", "desc", plugins=["/path/to/plugin"])
        assert json.loads(task["plugins"]) == ["/path/to/plugin"]

    def test_create_with_operating_brief(self, db):
        brief = {
            "objectives": ["find niches", "validate demand"],
            "workflows": ["research", "analyze", "report"],
            "capabilities": ["memory", "scheduling"],
        }
        task = store.create_task(db, "t2", "T2", "desc", operating_brief=brief)
        stored_brief = json.loads(task["operating_brief"])
        assert stored_brief["objectives"] == ["find niches", "validate demand"]
        assert stored_brief["capabilities"] == ["memory", "scheduling"]

    def test_create_without_brief_defaults_empty(self, db):
        task = store.create_task(db, "t3", "T3", "desc")
        stored_brief = json.loads(task["operating_brief"])
        assert stored_brief == {}

    def test_create_with_model(self, db):
        task = store.create_task(db, "t4", "T4", "desc", model="sonnet")
        assert task["model"] == "sonnet"

    def test_create_without_model_defaults_null(self, db):
        task = store.create_task(db, "t5", "T5", "desc")
        assert task["model"] is None

    def test_port_allocation_sequential(self, db):
        t1 = store.create_task(db, "a", "A", "desc")
        t2 = store.create_task(db, "b", "B", "desc")
        assert t2["port"] == t1["port"] + 1

    def test_duplicate_task_id_raises(self, db):
        store.create_task(db, "dup", "Dup", "desc")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_task(db, "dup", "Dup2", "desc2")


class TestGetAndList:
    def test_get_existing(self, db):
        store.create_task(db, "g1", "G1", "desc")
        task = store.get_task(db, "g1")
        assert task is not None
        assert task["name"] == "G1"

    def test_get_nonexistent(self, db):
        assert store.get_task(db, "nope") is None

    def test_list_all(self, db):
        store.create_task(db, "l1", "L1", "desc")
        store.create_task(db, "l2", "L2", "desc")
        tasks = store.list_tasks(db)
        assert len(tasks) == 2

    def test_list_by_status(self, db):
        store.create_task(db, "s1", "S1", "desc")
        store.create_task(db, "s2", "S2", "desc")
        store.update_status(db, "s1", "running")
        running = store.list_tasks(db, "running")
        assert len(running) == 1
        assert running[0]["task_id"] == "s1"


class TestStatusAndInvocation:
    def test_update_status(self, db):
        store.create_task(db, "u1", "U1", "desc")
        store.update_status(db, "u1", "running")
        task = store.get_task(db, "u1")
        assert task["status"] == "running"

    def test_increment_invocation(self, db):
        store.create_task(db, "i1", "I1", "desc")
        assert store.get_task(db, "i1")["invocation_count"] == 0
        store.increment_invocation(db, "i1")
        assert store.get_task(db, "i1")["invocation_count"] == 1
        store.increment_invocation(db, "i1")
        assert store.get_task(db, "i1")["invocation_count"] == 2


class TestLiveness:
    def test_last_seen_and_last_error_columns_exist(self, db):
        columns = [row[1] for row in db.execute("PRAGMA table_info(tasks)")]
        assert "last_seen_at" in columns
        assert "last_error" in columns

    def test_mark_seen_sets_timestamp(self, db):
        store.create_task(db, "ls1", "LS1", "desc")
        assert store.get_task(db, "ls1")["last_seen_at"] is None
        store.mark_seen(db, "ls1")
        assert store.get_task(db, "ls1")["last_seen_at"] is not None

    def test_mark_crashed_flips_status_and_records_error(self, db):
        store.create_task(db, "cr1", "CR1", "desc")
        store.update_status(db, "cr1", "running")
        store.mark_crashed(db, "cr1", "tmux session not found")
        task = store.get_task(db, "cr1")
        assert task["status"] == "crashed"
        assert task["last_error"] == "tmux session not found"

    def test_migration_adds_liveness_columns_to_old_schema(self, tmp_path):
        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE tasks (
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
        conn.close()

        conn = store.get_db(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]
        assert "last_seen_at" in columns
        assert "last_error" in columns
        conn.close()
