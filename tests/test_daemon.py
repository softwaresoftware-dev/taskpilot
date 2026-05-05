"""Tests for the supervisor daemon — scaffold endpoints."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
import daemon
import store

# Save reference before patching
_real_get_db = store.get_db


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def client(db_path):
    """TestClient with store pointed at a temp DB."""
    with patch("daemon.store.get_db", side_effect=lambda: _real_get_db(db_path)):
        yield TestClient(daemon.app)


class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["version"] == "0.1.0"
        assert body["supervised"] == 0
        assert body["total"] == 0


class TestListTasks:
    def test_empty(self, client):
        r = client.get("/tasks")
        assert r.status_code == 200
        assert r.json() == []

    def test_one_task(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test task", "desc", [], {}, None, None, [])
        conn.close()
        r = client.get("/tasks")
        assert r.status_code == 200
        tasks = r.json()
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "t1"


class TestGetTask:
    def test_not_found(self, client):
        r = client.get("/tasks/nope")
        assert r.status_code == 404

    def test_found(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test task", "desc", [], {}, None, None, [])
        conn.close()
        r = client.get("/tasks/t1")
        assert r.status_code == 200
        assert r.json()["task_id"] == "t1"


class TestSpawn:
    def test_404_when_unknown(self, client):
        r = client.post("/tasks/nope/spawn")
        assert r.status_code == 404

    def test_409_when_already_running(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [])
        store.update_status(conn, "t1", "running")
        conn.close()
        r = client.post("/tasks/t1/spawn")
        assert r.status_code == 409

    def test_501_for_kind_service(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [], kind="service")
        conn.close()
        r = client.post("/tasks/t1/spawn")
        assert r.status_code == 501

    def test_happy_path_calls_spawner(self, client, db_path):
        """Spawn should call spawn_tmux + send_initial_prompt + flip status."""
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [])
        conn.close()
        with (
            patch("daemon.spawner.spawn_tmux", return_value=True) as mock_spawn,
            patch("daemon.spawner.send_initial_prompt", return_value=True) as mock_prompt,
            patch("daemon.spawner.tmux_session_name", return_value="t1"),
        ):
            r = client.post("/tasks/t1/spawn")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "running"
        assert body["task_id"] == "t1"
        mock_spawn.assert_called_once()
        mock_prompt.assert_called_once()
        # status should now reflect 'running' in DB
        conn = _real_get_db(db_path)
        task = store.get_task(conn, "t1")
        conn.close()
        assert task["status"] == "running"

    def test_502_when_spawn_fails(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [])
        conn.close()
        with patch("daemon.spawner.spawn_tmux", return_value=False):
            r = client.post("/tasks/t1/spawn")
        assert r.status_code == 502


class TestKill:
    def test_404_when_unknown(self, client):
        r = client.post("/tasks/nope/kill")
        assert r.status_code == 404

    def test_501_for_kind_service(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [], kind="service")
        conn.close()
        r = client.post("/tasks/t1/kill")
        assert r.status_code == 501

    def test_happy_path_calls_spawner(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [])
        store.update_status(conn, "t1", "running")
        conn.close()
        with (
            patch("daemon.spawner.kill_tmux", return_value=True) as mock_kill,
            patch("daemon.spawner.cleanup_project_mcps") as mock_cleanup,
        ):
            r = client.post("/tasks/t1/kill")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "killed"
        assert body["tmux_killed"] is True
        mock_kill.assert_called_once_with("t1")
        mock_cleanup.assert_called_once_with("t1")
        conn = _real_get_db(db_path)
        task = store.get_task(conn, "t1")
        conn.close()
        assert task["status"] == "killed"


class TestMessage:
    def test_404_when_unknown(self, client):
        r = client.post("/tasks/nope/message", json={"text": "hi"})
        assert r.status_code == 404

    def test_502_when_channel_unhealthy(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [])
        conn.close()
        with patch("daemon.spawner.channel_healthy", return_value=False):
            r = client.post("/tasks/t1/message", json={"text": "hi"})
        assert r.status_code == 502

    def test_happy_path_invokes_curl(self, client, db_path):
        conn = _real_get_db(db_path)
        store.create_task(conn, "t1", "test", "desc", [], {}, None, None, [])
        conn.close()

        class FakeProc:
            returncode = 0
            stdout = "ok (chat_id: 7)"

        with (
            patch("daemon.spawner.channel_healthy", return_value=True),
            patch("daemon.subprocess.run", return_value=FakeProc()) as mock_run,
        ):
            r = client.post("/tasks/t1/message", json={"text": "hi"})
        assert r.status_code == 200
        body = r.json()
        assert body["delivered"] is True
        assert "ok" in body["response"]
        # confirm we called curl with the right URL
        cmd = mock_run.call_args[0][0]
        assert any("/sessions/t1/message" in arg for arg in cmd)
