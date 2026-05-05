"""Tests for the liveness reconciler.

The reconciler walks every task with status='running', checks tmux liveness,
and either stamps last_seen_at (alive) or flips to crashed (dead).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import liveness
import store


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    """Redirect ~/.taskpilot to a temp dir so events.jsonl writes don't pollute."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


def _seed_running(db_path: str, task_id: str) -> None:
    conn = store.get_db(db_path)
    store.create_task(conn, task_id, task_id, "desc")
    store.update_status(conn, task_id, "running")
    conn.close()


class TestReconcileOnce:
    def test_alive_task_gets_last_seen_stamped(self, db_path, state_root):
        _seed_running(db_path, "alive-task")

        with patch("spawner.is_tmux_alive", return_value=True):
            summary = liveness.reconcile_once(db_path)

        assert summary == {"checked": 1, "alive": 1, "crashed": []}
        conn = store.get_db(db_path)
        task = store.get_task(conn, "alive-task")
        conn.close()
        assert task["status"] == "running"
        assert task["last_seen_at"] is not None

    def test_dead_task_gets_marked_crashed(self, db_path, state_root):
        _seed_running(db_path, "dead-task")

        with patch("spawner.is_tmux_alive", return_value=False):
            summary = liveness.reconcile_once(db_path)

        assert summary["crashed"] == ["dead-task"]
        conn = store.get_db(db_path)
        task = store.get_task(conn, "dead-task")
        conn.close()
        assert task["status"] == "crashed"
        assert task["last_error"] == "tmux session not found"

    def test_crash_writes_event_log_entry(self, db_path, state_root):
        _seed_running(db_path, "logged-crash")

        with patch("spawner.is_tmux_alive", return_value=False):
            liveness.reconcile_once(db_path)

        events_path = state_root / ".taskpilot" / "logged-crash" / "state" / "events.jsonl"
        assert events_path.exists()
        lines = events_path.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["key"] == "liveness_crash"
        assert rec["previous_status"] == "running"
        assert "received_at" in rec

    def test_non_running_tasks_are_ignored(self, db_path, state_root):
        conn = store.get_db(db_path)
        store.create_task(conn, "completed-task", "completed-task", "desc")
        store.update_status(conn, "completed-task", "completed")
        conn.close()

        with patch("spawner.is_tmux_alive", return_value=False) as is_alive:
            summary = liveness.reconcile_once(db_path)

        assert summary == {"checked": 0, "alive": 0, "crashed": []}
        is_alive.assert_not_called()

    def test_mixed_set_returns_per_task_breakdown(self, db_path, state_root):
        _seed_running(db_path, "alive1")
        _seed_running(db_path, "alive2")
        _seed_running(db_path, "dead1")

        def fake_alive(tid):
            return tid.startswith("alive")

        with patch("spawner.is_tmux_alive", side_effect=fake_alive):
            summary = liveness.reconcile_once(db_path)

        assert summary["checked"] == 3
        assert summary["alive"] == 2
        assert summary["crashed"] == ["dead1"]
