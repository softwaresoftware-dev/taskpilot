#!/usr/bin/env python3
"""Taskpilot supervisor daemon.

A long-lived local service that owns the define/start/stop/message lifecycle
for tasks and exposes it over an HTTP API on :8912. The MCP server (server.py)
is a thin client over this daemon.

The API is resource-oriented and idempotent:

  - A task's *definition* (name, description, cwd, model, brief) is a resource
    you PUT. PUT twice is safe; the second call updates in place.
  - A task's *runtime* (is its tmux alive, is its channel registered) is
    observed ground truth, reported on every read and never assumed from the
    last lifecycle call. Reads reconcile the stored status against tmux: a
    "running" row whose tmux died reads (and persists) as "crashed"; a
    "stopped"/"crashed" row whose tmux is actually alive reads as "running".
  - start/stop are convergent verbs: "ensure running" / "ensure stopped".
    Calling start on a live task is a 200 no-op; calling start on a crashed
    task respawns it. Retrying either is always safe.
  - /message verifies delivery end-to-end: 409 when the agent is not running
    (callers can react by calling start), 503 when the agent is up but its
    channel hasn't registered yet (retryable), 502 when the channel POST
    failed. A 200 means session-bridge accepted the message for this agent.
  - DELETE stops the task and frees its id for reuse.

Status values: defined → running → (crashed | stopped | completed). Legacy
rows ('pending', 'killed') are migrated on open by store._ensure_schema.

Deprecated aliases kept for one release (old consumers: dispatcher,
crestborne, cool-af, voice-lab): POST /tasks/{id}/spawn → start,
POST /tasks/{id}/kill → stop, POST /tasks/create_and_spawn → PUT + start.

The daemon installs as a boot-persistence service through the daemon
capability (daemon-manager); see `skills/setup/SKILL.md`. It is reactive:
it acts on API calls, not on a background timer.
"""

import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# We're a sibling of server.py / spawner.py / store.py
sys.path.insert(0, str(Path(__file__).parent))
import spawner
import store

DEFAULT_PORT = 8912
VERSION = "0.15.0"

log = logging.getLogger("taskpilot.daemon")


# --- Models ---


class HealthResponse(BaseModel):
    ok: bool
    version: str
    running: int
    total: int


class TaskDefinition(BaseModel):
    """The desired definition of a task — the body of PUT /tasks/{id}."""
    description: str
    name: str | None = None
    cwd: str | None = None
    model: str | None = None
    brief: dict | None = None
    plugins: list[str] | None = None


class StartRequest(BaseModel):
    """Optional start parameters. `prompt` overrides the starter prompt for
    this start only (the stored description is the default). A reviver
    (e.g. the mindframe dashboard respawning a frame agent) passes a
    resume-flavored prompt here so the agent doesn't redo its first turn."""
    prompt: str | None = None


class MessageRequest(BaseModel):
    text: str
    from_session: str | None = None


class CreateSpawnRequest(BaseModel):
    """Deprecated composite (PUT + start) for callers that want one round
    trip (the dispatcher's `spawn:<recipe>` path). Now idempotent: an
    existing task is updated and ensured running instead of 409ing."""
    description: str
    name: str | None = None
    cwd: str | None = None
    model: str | None = None
    brief: dict | None = None


app = FastAPI(title="taskpilot-daemon")


# --- Per-task locking ---
#
# start/stop/delete mutate tmux + the DB row; two concurrent starts for the
# same task must not race into a double spawn. Endpoints run in FastAPI's
# threadpool, so a plain threading.Lock per task id is enough.

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _task_lock(task_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(task_id, threading.Lock())


# --- Helpers ---


def _get_or_404(task_id: str) -> dict:
    with store.db() as conn:
        task = store.get_task(conn, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    return task


def _reconcile(task: dict) -> dict:
    """Make the stored status agree with tmux ground truth. Mutates and
    returns the task dict; persists any correction.

      running + dead tmux  -> crashed   (the agent died; nothing noticed)
      stopped/crashed + live tmux -> running  (an external kill failed, or a
                                               stop raced a spawn)
    """
    alive = spawner.is_tmux_alive(task["task_id"])
    status = task["status"]
    corrected = None
    if status == "running" and not alive:
        corrected = "crashed"
    elif status in ("stopped", "crashed") and alive:
        corrected = "running"
    if corrected:
        with store.db() as conn:
            store.update_status(conn, task["task_id"], corrected)
        task["status"] = corrected
    task["tmux_alive"] = alive
    return task


def _enrich(task: dict) -> dict:
    """Reconcile status with ground truth and add live health fields."""
    _reconcile(task)
    task["channel_healthy"] = spawner.channel_healthy(task["task_id"])
    return task


def _read_state(task_id: str) -> dict | None:
    """Read state.json for a task, returning None if absent or malformed."""
    state_file = spawner.task_dir(task_id) / "state.json"
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text())
    except json.JSONDecodeError:
        return {"error": "malformed state.json"}


def _upsert(task_id: str, body: TaskDefinition) -> tuple[dict, bool]:
    """Create or update a task definition row + its config files.
    Returns (task, created)."""
    name = body.name or body.description[:80]
    with store.db() as conn:
        existing = store.get_task(conn, task_id)
        if existing:
            store.update_definition(
                conn, task_id, name=name, description=body.description,
                plugins=body.plugins, operating_brief=body.brief,
                model=body.model, cwd=body.cwd,
            )
            task = store.get_task(conn, task_id)
            created = False
        else:
            task = store.create_task(
                conn, task_id, name, body.description,
                body.plugins or [], body.brief or {}, body.model, body.cwd,
            )
            created = True
    spawner.write_task_config(task_id, name, body.description,
                              body.plugins or [], body.brief or {})
    return task, created


def _start(task_id: str, prompt: str | None) -> dict:
    """Ensure the task's agent is running. Idempotent: a live agent is a
    no-op; a dead/never-started one is (re)spawned and sent its starter
    prompt — `prompt` if given, else the stored description."""
    task = _get_or_404(task_id)

    with _task_lock(task_id):
        if spawner.is_tmux_alive(task_id):
            with store.db() as conn:
                store.update_status(conn, task_id, "running")
            return {
                "ok": True,
                "task_id": task_id,
                "status": "running",
                "started": False,
                "already_running": True,
                "tmux_session": spawner.tmux_session_name(task_id),
                "channel_healthy": spawner.channel_healthy(task_id),
            }

        # Clear any half-dead session state before respawning (a dead tmux
        # can't be present here, but a name-squatting zombie pane can).
        spawner.kill_tmux(task_id)

        # spawn_tmux blocks ~16s — that's fine, we hold only this task's lock.
        plugins = json.loads(task["plugins"]) if task["plugins"] else []
        success = spawner.spawn_tmux(
            task_id, plugins, model=task.get("model"), cwd=task.get("cwd"),
        )
        if not success:
            raise HTTPException(
                status_code=502,
                detail=f"start failed for {task_id}: tmux session could not be launched",
            )

        with store.db() as conn:
            store.update_status(conn, task_id, "running")
            store.increment_invocation(conn, task_id)

        starter = prompt if prompt is not None else task["description"]
        prompt_delivered = spawner.send_initial_prompt(task_id, starter)
        if not prompt_delivered:
            log.warning("start %s: agent up but starter prompt was not delivered", task_id)

    return {
        "ok": True,
        "task_id": task_id,
        "status": "running",
        "started": True,
        "already_running": False,
        "prompt_delivered": prompt_delivered,
        "tmux_session": spawner.tmux_session_name(task_id),
        "channel_healthy": spawner.channel_healthy(task_id),
    }


def _stop(task_id: str) -> dict:
    """Ensure the task's agent is stopped. Idempotent — stopping a dead or
    never-started task is a 200 no-op."""
    task = _get_or_404(task_id)
    with _task_lock(task_id):
        tmux_killed = spawner.kill_tmux(task_id)
        spawner.cleanup_project_mcps(task_id)
        # Only a task that has actually run becomes "stopped"; a defined or
        # completed row keeps its status.
        new_status = task["status"]
        if task["status"] in ("running", "crashed"):
            new_status = "stopped"
            with store.db() as conn:
                store.update_status(conn, task_id, "stopped")
    return {
        "ok": True,
        "task_id": task_id,
        "status": new_status,
        "tmux_killed": tmux_killed,
    }


# --- Read endpoints ---


@app.get("/health")
def health() -> HealthResponse:
    """Daemon health + how many tasks are marked running."""
    with store.db() as conn:
        running = store.list_tasks(conn, "running")
        everything = store.list_tasks(conn)
    return HealthResponse(
        ok=True,
        version=VERSION,
        running=len(running),
        total=len(everything),
    )


@app.get("/tasks")
def list_tasks(status: str | None = None) -> list[dict]:
    """List tasks with reconciled status + live health. Optional ?status=
    filter (applied after reconciliation, so it filters on the truth)."""
    with store.db() as conn:
        tasks = store.list_tasks(conn)
    enriched = [_enrich(t) for t in tasks]
    if status:
        enriched = [t for t in enriched if t["status"] == status]
    return enriched


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    """Full task detail with reconciled status, live health, and state.json."""
    task = _get_or_404(task_id)
    _enrich(task)
    task["state"] = _read_state(task_id)
    return task


# --- Write endpoints ---


@app.put("/tasks/{task_id}")
def put_task(task_id: str, body: TaskDefinition) -> dict:
    """Create or update a task definition. Idempotent — the task id is the
    caller-chosen identity; PUTting it again updates the definition in place
    (a running agent is not re-prompted; the new definition applies from the
    next start)."""
    if spawner.slugify(task_id) != task_id or not task_id:
        raise HTTPException(
            status_code=422,
            detail=f"task id '{task_id}' must be a slug ([a-z0-9-], max 50 chars)",
        )
    task, created = _upsert(task_id, body)
    task["created"] = created
    return task


@app.post("/tasks/{task_id}/start")
def start(task_id: str, body: StartRequest | None = None) -> dict:
    """Ensure the task's agent is running (idempotent). Optional body
    {prompt} overrides the starter prompt for this start."""
    return _start(task_id, body.prompt if body else None)


@app.post("/tasks/{task_id}/stop")
def stop(task_id: str) -> dict:
    """Ensure the task's agent is stopped (idempotent)."""
    return _stop(task_id)


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str) -> dict:
    """Stop the task and delete it — row, config dir, and all. Frees the id
    for reuse. Idempotent: deleting an absent task is a 200 no-op."""
    with store.db() as conn:
        task = store.get_task(conn, task_id)
    if not task:
        return {"ok": True, "task_id": task_id, "deleted": False, "existed": False}
    with _task_lock(task_id):
        spawner.kill_tmux(task_id)
        spawner.cleanup_project_mcps(task_id)
        with store.db() as conn:
            store.delete_task(conn, task_id)
        shutil.rmtree(spawner.task_dir(task_id), ignore_errors=True)
    return {"ok": True, "task_id": task_id, "deleted": True, "existed": True}


@app.post("/tasks/{task_id}/message")
def message(task_id: str, body: MessageRequest) -> dict:
    """Deliver a message to a running task via session-bridge, verifying
    delivery. Error contract (machine-readable `detail.code`):

      409 agent_not_running — tmux is dead; caller may POST /start and retry
      503 channel_not_ready — agent alive but channel unregistered; retry soon
      502 delivery_failed   — session-bridge rejected/failed the forward
    """
    task = _get_or_404(task_id)
    _reconcile(task)

    if not task["tmux_alive"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "agent_not_running",
                "task_status": task["status"],
                "message": f"task '{task_id}' has no live agent — POST /tasks/{task_id}/start to revive it",
            },
        )
    if not spawner.channel_healthy(task_id):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "channel_not_ready",
                "message": f"task '{task_id}' is up but its channel is not registered with session-bridge yet — retry shortly",
            },
        )

    delivered = spawner.post_to_channel(
        task_id, body.text, body.from_session or "taskpilot-daemon"
    )
    if not delivered:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "delivery_failed",
                "message": f"session-bridge did not accept the message for '{task_id}'",
            },
        )
    return {"ok": True, "delivered": True, "task_id": task_id}


# --- Deprecated aliases (kept for one release) ---


@app.post("/tasks/{task_id}/spawn", deprecated=True)
def spawn_alias(task_id: str) -> dict:
    """Deprecated alias for POST /tasks/{id}/start."""
    return _start(task_id, None)


@app.post("/tasks/{task_id}/kill", deprecated=True)
def kill_alias(task_id: str) -> dict:
    """Deprecated alias for POST /tasks/{id}/stop."""
    return _stop(task_id)


@app.post("/tasks/create_and_spawn", deprecated=True)
def create_and_spawn(body: CreateSpawnRequest) -> dict:
    """Deprecated composite: PUT /tasks/{id} + POST /tasks/{id}/start in one
    round trip. The task_id is slugified from `name` (or the description), so
    callers that predict the id locally get the same value back. Idempotent:
    re-posting updates the definition and ensures the agent is running (an
    already-running agent is NOT re-prompted)."""
    task_id = spawner.slugify(body.name or body.description[:80])
    _upsert(task_id, TaskDefinition(
        description=body.description, name=body.name,
        cwd=body.cwd, model=body.model, brief=body.brief,
    ))
    return _start(task_id, None)


# --- Boot-persistence service ---
#
# taskpilot no longer renders its own systemd unit / launchd plist. The boot
# service is installed and version-drift-healed by the `daemon` capability
# provider (daemon-manager >= 1.5.0), which can now emit the two directives
# taskpilot needs:
#   - KillMode=process (systemd) / AbandonProcessGroup (launchd) — so a daemon
#     restart does not tear down the detached tmux agents it spawned.
#   - After=/Wants=session-bridge.service — startup ordering behind the mesh.
# Registration lives in `skills/setup/SKILL.md`, which calls daemon_start +
# daemon_install_autostart with kill_mode/after/wants. Running through
# daemon-manager is what gets taskpilot auto-restarted when the plugin updates.


def _install_pointer() -> None:
    print(
        "taskpilot's boot service is now managed by the daemon capability "
        "(daemon-manager), not by `daemon.py --install`.\n"
        "Install/repair it from Claude Code with:  /taskpilot:setup\n"
        "To run the daemon in the foreground for development:  python daemon.py",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Entry point ---


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--install", "--uninstall"):
        _install_pointer()
        return

    port = int(os.environ.get("TASKPILOT_DAEMON_PORT", DEFAULT_PORT))
    bind = os.environ.get("TASKPILOT_DAEMON_BIND", "127.0.0.1")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    uvicorn.run(app, host=bind, port=port, log_level="info")


if __name__ == "__main__":
    main()
