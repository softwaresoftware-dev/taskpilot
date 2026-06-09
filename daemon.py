#!/usr/bin/env python3
"""Taskpilot supervisor daemon.

A long-lived local service that owns the spawn/kill/message lifecycle for
tasks and exposes it over an HTTP API on :8912. The MCP server (server.py)
is a thin client over this daemon.

The daemon installs as a boot-persistence service through the daemon
capability (daemon-manager); see `skills/setup/SKILL.md`. It is reactive:
it acts on API calls, not on a background timer. Liveness (is the agent's
tmux still alive) is reported on-demand when a task is listed or fetched.
"""

import json
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# We're a sibling of server.py / spawner.py / store.py
sys.path.insert(0, str(Path(__file__).parent))
import spawner
import store

DEFAULT_PORT = 8912

log = logging.getLogger("taskpilot.daemon")


# --- Models ---


class HealthResponse(BaseModel):
    ok: bool
    version: str
    running: int
    total: int


class MessageRequest(BaseModel):
    text: str
    from_session: str | None = None


class CreateSpawnRequest(BaseModel):
    """One-shot create-and-spawn for event-driven callers (e.g. dispatcher)
    that have no prior task row. Collapses the MCP define_task + spawn_task
    pair into a single HTTP round-trip."""
    description: str
    name: str | None = None
    cwd: str | None = None
    model: str | None = None
    brief: dict | None = None


app = FastAPI(title="taskpilot-daemon")


# --- Read endpoints ---


@app.get("/health")
def health() -> HealthResponse:
    """Daemon health + how many tasks are marked running."""
    with store.db() as conn:
        running = store.list_tasks(conn, "running")
        everything = store.list_tasks(conn)
    return HealthResponse(
        ok=True,
        version="0.1.0",
        running=len(running),
        total=len(everything),
    )


def _enrich(task: dict) -> dict:
    """Add live health fields to a task row. Mutates and returns."""
    tid = task["task_id"]
    task["tmux_alive"] = spawner.is_tmux_alive(tid)
    task["channel_healthy"] = spawner.channel_healthy(tid)
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


@app.get("/tasks")
def list_tasks(status: str | None = None) -> list[dict]:
    """List tasks with live health. Optional ?status= filter."""
    with store.db() as conn:
        tasks = store.list_tasks(conn, status)
    return [_enrich(t) for t in tasks]


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    """Full task detail with live health and state.json."""
    with store.db() as conn:
        task = store.get_task(conn, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    _enrich(task)
    task["state"] = _read_state(task_id)
    return task


# --- Write endpoints ---


@app.post("/tasks/{task_id}/spawn")
def spawn(task_id: str) -> dict:
    """Spawn a task in tmux."""
    with store.db() as conn:
        task = store.get_task(conn, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
        if task["status"] == "running":
            raise HTTPException(status_code=409, detail=f"task '{task_id}' is already running")

    # spawn_tmux blocks ~16s — do it outside any open DB connection.
    plugins = json.loads(task["plugins"]) if task["plugins"] else []
    success = spawner.spawn_tmux(
        task_id, plugins, model=task.get("model"), cwd=task.get("cwd"),
    )
    if not success:
        raise HTTPException(status_code=502, detail=f"spawn failed for {task_id}: tmux session could not be launched")

    with store.db() as conn:
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)

    spawner.send_initial_prompt(task_id, task["description"])

    return {
        "status": "running",
        "task_id": task_id,
        "tmux_session": spawner.tmux_session_name(task_id),
        "channel_healthy": spawner.channel_healthy(task_id),
    }


@app.post("/tasks/create_and_spawn")
def create_and_spawn(body: CreateSpawnRequest) -> dict:
    """Create a task and immediately spawn it in one call.

    Mirrors the MCP `define_task` + `spawn_task` pair for callers that don't
    hold a prior task row (the dispatcher's `spawn:<recipe>` path). The task_id
    is slugified from `name` (or the description) — passing an already-slugged
    name is idempotent, so callers that predict the task_id locally get the
    same value back.
    """
    name = body.name or body.description[:80]
    task_id = spawner.slugify(name)

    with store.db() as conn:
        existing = store.get_task(conn, task_id)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"task '{task_id}' already exists with status '{existing['status']}'",
            )
        store.create_task(
            conn, task_id, name, body.description,
            None, body.brief or {}, body.model, body.cwd,
        )
    spawner.write_task_config(task_id, name, body.description, [], body.brief or {})

    # spawn_tmux blocks ~16s — do it outside any open DB connection.
    success = spawner.spawn_tmux(
        task_id, [], model=body.model, cwd=body.cwd,
    )
    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"spawn failed for {task_id}: tmux session could not be launched",
        )

    with store.db() as conn:
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)

    spawner.send_initial_prompt(task_id, body.description)

    return {
        "ok": True,
        "status": "running",
        "task_id": task_id,
        "tmux_session": spawner.tmux_session_name(task_id),
        "channel_healthy": spawner.channel_healthy(task_id),
    }


@app.post("/tasks/{task_id}/kill")
def kill(task_id: str) -> dict:
    """Kill a running task — stop its tmux session and clean up project MCPs."""
    with store.db() as conn:
        task = store.get_task(conn, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
        tmux_killed = spawner.kill_tmux(task_id)
        spawner.cleanup_project_mcps(task_id)
        store.update_status(conn, task_id, "killed")

    return {
        "task_id": task_id,
        "status": "killed",
        "tmux_killed": tmux_killed,
    }


@app.post("/tasks/{task_id}/message")
def message(task_id: str, body: MessageRequest) -> dict:
    """Forward a message to a running task via session-bridge."""
    with store.db() as conn:
        task = store.get_task(conn, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")

    if not spawner.channel_healthy(task_id):
        raise HTTPException(
            status_code=502,
            detail=f"task '{task_id}' channel not reachable via session-bridge",
        )

    delivered = spawner.post_to_channel(
        task_id, body.text, body.from_session or "taskpilot-daemon"
    )
    return {"delivered": delivered}


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
