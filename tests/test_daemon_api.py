"""Daemon API contract tests — the idempotency and truth-telling invariants.

The spawner is replaced by an in-memory fake (a set of "alive" tmux sessions),
and the store points at a temp SQLite file. No tmux, no session-bridge, no
network. These tests pin the 0.15.0 API contract:

  - PUT is upsert; ids are caller-chosen and validated.
  - start/stop converge (retry-safe, no 409-because-already-there).
  - reads reconcile stored status against tmux ground truth.
  - /message verifies delivery and fails with actionable codes.
  - DELETE frees the id for reuse.
  - legacy status values migrate on open.
"""

import importlib.util
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import spawner  # noqa: E402
import store  # noqa: E402

_spec = importlib.util.spec_from_file_location("taskpilot_daemon_under_test", ROOT / "daemon.py")
daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon)

client = TestClient(daemon.app)


class FakeRuntime:
    """Stands in for tmux + session-bridge. `alive` is the set of live tmux
    sessions; `channels` the set of bridge-registered ones."""

    def __init__(self):
        self.alive: set[str] = set()
        self.channels: set[str] = set()
        self.spawn_ok = True
        self.post_ok = True
        self.prompts: list[tuple[str, str]] = []

    def is_tmux_alive(self, task_id):
        return task_id in self.alive

    def spawn_tmux(self, task_id, plugins, model=None, cwd=None):
        if not self.spawn_ok:
            return False
        self.alive.add(task_id)
        self.channels.add(task_id)
        return True

    def kill_tmux(self, task_id):
        was = task_id in self.alive
        self.alive.discard(task_id)
        self.channels.discard(task_id)
        return was

    def channel_healthy(self, task_id):
        return task_id in self.channels

    def post_to_channel(self, task_id, text, from_session):
        return self.post_ok

    def send_initial_prompt(self, task_id, description):
        self.prompts.append((task_id, description))
        return self.post_ok


@pytest.fixture()
def rt(tmp_path, monkeypatch):
    fake = FakeRuntime()
    for fn in ("is_tmux_alive", "spawn_tmux", "kill_tmux", "channel_healthy",
               "post_to_channel", "send_initial_prompt"):
        monkeypatch.setattr(spawner, fn, getattr(fake, fn))
    monkeypatch.setattr(spawner, "write_task_config", lambda *a, **k: tmp_path)
    monkeypatch.setattr(spawner, "cleanup_project_mcps", lambda task_id: None)
    monkeypatch.setattr(spawner, "task_dir", lambda task_id: tmp_path / task_id)
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", tmp_path / "test.db")
    daemon._locks.clear()
    return fake


def _put(tid="job-1", desc="do the thing", **extra):
    return client.put(f"/tasks/{tid}", json={"description": desc, **extra})


# --------------------------- PUT (upsert) ---------------------------


def test_put_creates_then_updates(rt):
    r = _put(desc="v1")
    assert r.status_code == 200 and r.json()["created"] is True
    assert r.json()["status"] == "defined"
    r = _put(desc="v2")
    assert r.status_code == 200 and r.json()["created"] is False
    assert client.get("/tasks/job-1").json()["description"] == "v2"


def test_put_rejects_non_slug_ids(rt):
    assert client.put("/tasks/Not%20A%20Slug", json={"description": "x"}).status_code == 422


# --------------------------- start (ensure running) ---------------------------


def test_start_spawns_and_sends_description(rt):
    _put(desc="the brief")
    r = client.post("/tasks/job-1/start")
    j = r.json()
    assert r.status_code == 200 and j["started"] is True
    assert rt.prompts == [("job-1", "the brief")]
    assert client.get("/tasks/job-1").json()["status"] == "running"


def test_start_on_running_task_is_noop(rt):
    _put()
    client.post("/tasks/job-1/start")
    r = client.post("/tasks/job-1/start")
    j = r.json()
    assert r.status_code == 200 and j["already_running"] is True and j["started"] is False
    assert len(rt.prompts) == 1  # no second prompt


def test_start_revives_a_crashed_task_with_prompt_override(rt):
    _put(desc="original brief")
    client.post("/tasks/job-1/start")
    rt.alive.clear()  # the agent dies
    rt.channels.clear()
    r = client.post("/tasks/job-1/start", json={"prompt": "resume where you left off"})
    assert r.status_code == 200 and r.json()["started"] is True
    assert rt.prompts[-1] == ("job-1", "resume where you left off")


def test_start_unknown_task_404s(rt):
    assert client.post("/tasks/ghost/start").status_code == 404


def test_start_spawn_failure_is_502(rt):
    _put()
    rt.spawn_ok = False
    assert client.post("/tasks/job-1/start").status_code == 502


# --------------------------- status reconciliation ---------------------------


def test_dead_tmux_reads_as_crashed(rt):
    _put()
    client.post("/tasks/job-1/start")
    rt.alive.clear()
    j = client.get("/tasks/job-1").json()
    assert j["status"] == "crashed" and j["tmux_alive"] is False
    # and the correction persisted
    assert client.get("/tasks/job-1").json()["status"] == "crashed"


def test_zombie_tmux_reads_as_running(rt):
    _put()
    client.post("/tasks/job-1/start")
    client.post("/tasks/job-1/stop")
    rt.alive.add("job-1")  # the kill didn't take (or an external respawn)
    assert client.get("/tasks/job-1").json()["status"] == "running"


def test_list_filters_on_reconciled_status(rt):
    _put("a")
    _put("b")
    client.post("/tasks/a/start")
    client.post("/tasks/b/start")
    rt.alive.discard("b")
    running = client.get("/tasks?status=running").json()
    crashed = client.get("/tasks?status=crashed").json()
    assert [t["task_id"] for t in running] == ["a"]
    assert [t["task_id"] for t in crashed] == ["b"]


# --------------------------- stop (ensure stopped) ---------------------------


def test_stop_is_idempotent(rt):
    _put()
    client.post("/tasks/job-1/start")
    r1 = client.post("/tasks/job-1/stop")
    r2 = client.post("/tasks/job-1/stop")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["tmux_killed"] is True and r2.json()["tmux_killed"] is False
    assert client.get("/tasks/job-1").json()["status"] == "stopped"


def test_stop_on_never_started_task_keeps_defined(rt):
    _put()
    r = client.post("/tasks/job-1/stop")
    assert r.status_code == 200 and r.json()["status"] == "defined"


# --------------------------- message (verified delivery) ---------------------------


def test_message_delivers_when_running(rt):
    _put()
    client.post("/tasks/job-1/start")
    r = client.post("/tasks/job-1/message", json={"text": "hi"})
    assert r.status_code == 200 and r.json()["delivered"] is True


def test_message_to_dead_agent_is_409_with_code(rt):
    _put()
    client.post("/tasks/job-1/start")
    rt.alive.clear()
    r = client.post("/tasks/job-1/message", json={"text": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "agent_not_running"
    assert r.json()["detail"]["task_status"] == "crashed"


def test_message_while_channel_unregistered_is_503(rt):
    _put()
    client.post("/tasks/job-1/start")
    rt.channels.clear()  # alive, but bridge hasn't seen it
    r = client.post("/tasks/job-1/message", json={"text": "hi"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "channel_not_ready"


def test_message_failed_forward_is_502(rt):
    _put()
    client.post("/tasks/job-1/start")
    rt.post_ok = False
    r = client.post("/tasks/job-1/message", json={"text": "hi"})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "delivery_failed"


# --------------------------- delete (free the id) ---------------------------


def test_delete_frees_the_id(rt):
    _put()
    client.post("/tasks/job-1/start")
    r = client.delete("/tasks/job-1")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert "job-1" not in rt.alive  # agent stopped too
    assert client.get("/tasks/job-1").status_code == 404
    assert _put().json()["created"] is True  # id reusable


def test_delete_absent_task_is_noop(rt):
    r = client.delete("/tasks/ghost")
    assert r.status_code == 200 and r.json()["existed"] is False


# --------------------------- deprecated aliases ---------------------------


def test_create_and_spawn_is_idempotent(rt):
    body = {"description": "brief", "name": "evt-1"}
    r1 = client.post("/tasks/create_and_spawn", json=body)
    r2 = client.post("/tasks/create_and_spawn", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["task_id"] == r2.json()["task_id"] == "evt-1"
    assert r2.json()["already_running"] is True
    assert len(rt.prompts) == 1


def test_spawn_and_kill_aliases_map_to_start_stop(rt):
    _put()
    assert client.post("/tasks/job-1/spawn").json()["started"] is True
    assert client.post("/tasks/job-1/spawn").json()["already_running"] is True
    assert client.post("/tasks/job-1/kill").json()["status"] == "stopped"


# --------------------------- legacy migration ---------------------------


def test_legacy_statuses_migrate_on_open(rt):
    with store.db() as conn:
        store.create_task(conn, "old-1", "old-1", "x")
        store.create_task(conn, "old-2", "old-2", "x")
        conn.execute("UPDATE tasks SET status='pending' WHERE task_id='old-1'")
        conn.execute("UPDATE tasks SET status='killed' WHERE task_id='old-2'")
        conn.commit()
    assert client.get("/tasks/old-1").json()["status"] == "defined"
    assert client.get("/tasks/old-2").json()["status"] == "stopped"
