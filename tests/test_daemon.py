"""Tests for the supervisor daemon — endpoints + reconciler."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_kind_service_spawns_same_path(self, client, db_path):
        """As of phase 2, kind=service goes through the same spawn_tmux call.
        Reconciler treatment differs, not the spawn itself."""
        conn = _real_get_db(db_path)
        store.create_task(conn, "svc", "test", "desc", [], {}, None, None, [], kind="service")
        conn.close()
        with (
            patch("daemon.spawner.spawn_tmux", return_value=True) as mock_spawn,
            patch("daemon.spawner.send_initial_prompt", return_value=True),
            patch("daemon.spawner.tmux_session_name", return_value="svc"),
        ):
            r = client.post("/tasks/svc/spawn")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "service"
        mock_spawn.assert_called_once()

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

    def test_kind_service_kill_works(self, client, db_path):
        """Phase 2: /kill works for kind=service too. Reconciler stops touching
        the task once status=killed."""
        conn = _real_get_db(db_path)
        store.create_task(conn, "svc", "test", "desc", [], {}, None, None, [], kind="service")
        store.update_status(conn, "svc", "running")
        conn.close()
        with (
            patch("daemon.spawner.kill_tmux", return_value=True),
            patch("daemon.spawner.cleanup_project_mcps"),
        ):
            r = client.post("/tasks/svc/kill")
        assert r.status_code == 200
        assert r.json()["status"] == "killed"

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


class TestReconciler:
    """Phase 2: the reconciler is the supervisor.

    Service-kind tasks whose tmux died get respawned. Task-kind tasks whose
    tmux died get marked crashed. Alive tasks just get their last_seen_at
    stamped. Killed/completed tasks aren't touched (filter is status=running).
    """

    @pytest.fixture
    def db_with_tasks(self, tmp_path):
        """Fresh DB seeded with one of each kind+state we care about."""
        db_path = str(tmp_path / "test.db")
        conn = _real_get_db(db_path)
        # service that we'll mark as needing respawn
        store.create_task(conn, "svc-dead", "svc", "desc", [], {}, None, None, [], kind="service")
        store.update_status(conn, "svc-dead", "running")
        # service that is alive — gets last_seen stamped
        store.create_task(conn, "svc-alive", "svc", "desc", [], {}, None, None, [], kind="service")
        store.update_status(conn, "svc-alive", "running")
        # task-kind, dead — gets crashed
        store.create_task(conn, "task-dead", "t", "desc", [], {}, None, None, [], kind="task")
        store.update_status(conn, "task-dead", "running")
        # killed task — should be ignored entirely
        store.create_task(conn, "killed", "k", "desc", [], {}, None, None, [])
        store.update_status(conn, "killed", "killed")
        conn.close()
        return db_path

    def test_reconcile_respawns_dead_services(self, db_with_tasks):
        """Service whose tmux is dead → reconcile_once calls spawn_tmux."""
        # Only the "svc-dead" entry should be alive=False. Others alive=True
        # except killed which isn't queried.
        def fake_alive(task_id):
            return task_id != "svc-dead" and task_id != "task-dead"

        with (
            patch("daemon.store.get_db", side_effect=lambda: _real_get_db(db_with_tasks)),
            patch("daemon.spawner.is_tmux_alive", side_effect=fake_alive),
            patch("daemon.spawner.spawn_tmux", return_value=True) as mock_spawn,
        ):
            counts = daemon.reconcile_once()

        assert counts["checked"] == 3  # killed isn't checked
        assert counts["alive"] == 1    # svc-alive
        assert counts["respawned"] == 1
        assert counts["crashed"] == 1
        # spawn_tmux must have been called for svc-dead specifically
        assert mock_spawn.called
        called_id = mock_spawn.call_args[0][0]
        assert called_id == "svc-dead"

    def test_reconcile_marks_dead_tasks_crashed(self, db_with_tasks):
        """task-kind whose tmux is dead → status flips to crashed (no respawn)."""
        def fake_alive(task_id):
            return False  # everything dead — but we'll inspect each kind separately

        with (
            patch("daemon.store.get_db", side_effect=lambda: _real_get_db(db_with_tasks)),
            patch("daemon.spawner.is_tmux_alive", side_effect=fake_alive),
            patch("daemon.spawner.spawn_tmux", return_value=True),
        ):
            daemon.reconcile_once()

        # task-dead should now be crashed
        conn = _real_get_db(db_with_tasks)
        t = store.get_task(conn, "task-dead")
        conn.close()
        assert t["status"] == "crashed"

    def test_reconcile_ignores_non_running(self, db_with_tasks):
        """Killed/completed tasks aren't touched."""
        def fake_alive(task_id):
            return False

        with (
            patch("daemon.store.get_db", side_effect=lambda: _real_get_db(db_with_tasks)),
            patch("daemon.spawner.is_tmux_alive", side_effect=fake_alive),
            patch("daemon.spawner.spawn_tmux", return_value=True),
        ):
            daemon.reconcile_once()

        conn = _real_get_db(db_with_tasks)
        k = store.get_task(conn, "killed")
        conn.close()
        assert k["status"] == "killed"

    def test_reconcile_marks_failed_when_spawn_fails(self, db_with_tasks):
        """Service respawn that fails → reconcile counts it under 'failed'
        and flips status to crashed so we don't infinite-loop on broken config."""
        def fake_alive(task_id):
            return task_id != "svc-dead"

        with (
            patch("daemon.store.get_db", side_effect=lambda: _real_get_db(db_with_tasks)),
            patch("daemon.spawner.is_tmux_alive", side_effect=fake_alive),
            patch("daemon.spawner.spawn_tmux", return_value=False),  # spawn fails
        ):
            counts = daemon.reconcile_once()

        assert counts["failed"] == 1
        conn = _real_get_db(db_with_tasks)
        t = store.get_task(conn, "svc-dead")
        conn.close()
        assert t["status"] == "crashed"
