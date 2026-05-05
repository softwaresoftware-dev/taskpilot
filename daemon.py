#!/usr/bin/env python3
"""Taskpilot supervisor daemon.

Long-lived process that owns the spawn/kill/respawn lifecycle for tasks.
The MCP server (server.py) is a thin client over this daemon's HTTP API.

Phases:
  0. Scaffold (this file). Endpoints exist; spawn/kill/message return 501
     until phase 1 wires them.
  1. Route new spawns through daemon (spawner.spawn_tmux delegates here).
  2. Migrate existing kind=service tasks off per-task systemd units.
  3. Cleanup — drop start.sh, rotation.py, liveness.py.

Why a daemon at all: see the bigger refactor note in CLAUDE.md / vault. Short
version: a Claude-session-scoped MCP can't outlive its parent, so per-task
systemd units became the de-facto supervisors. Template drift, liveness
fictions, and split state are the price. This daemon collapses those concerns
into one process.
"""

import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# We're a sibling of server.py / spawner.py / store.py
sys.path.insert(0, str(Path(__file__).parent))
import spawner
import store

DEFAULT_PORT = 8912
TASKPILOT_DIR = Path.home() / ".taskpilot"

log = logging.getLogger("taskpilot.daemon")


# --- Models ---


class HealthResponse(BaseModel):
    ok: bool
    version: str
    supervised: int
    total: int


class MessageRequest(BaseModel):
    text: str
    from_session: str | None = None


# --- Lifespan ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("taskpilot daemon starting")
    # Phase 0: nothing to reconcile. Phase 1 will start a heartbeat tick here.
    yield
    log.info("taskpilot daemon stopping")


app = FastAPI(title="taskpilot-daemon", lifespan=lifespan)


# --- Read endpoints (live in phase 0) ---


@app.get("/health")
def health() -> HealthResponse:
    """Daemon health + how many tasks are under our supervision."""
    conn = store.get_db()
    running = store.list_tasks(conn, "running")
    everything = store.list_tasks(conn)
    conn.close()
    return HealthResponse(
        ok=True,
        version="0.1.0",
        supervised=len(running),
        total=len(everything),
    )


@app.get("/tasks")
def list_tasks(status: str | None = None) -> list[dict]:
    """List tasks. Optional ?status= filter."""
    conn = store.get_db()
    tasks = store.list_tasks(conn, status)
    conn.close()
    for t in tasks:
        t["tmux_alive"] = spawner.is_tmux_alive(t["task_id"])
        t["channel_healthy"] = spawner.channel_healthy(t["task_id"])
    return tasks


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    """Full task detail."""
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()
    if not task:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    task["tmux_alive"] = spawner.is_tmux_alive(task_id)
    task["channel_healthy"] = spawner.channel_healthy(task_id)
    return task


@app.get("/tasks/{task_id}/log")
def get_log(task_id: str, lines: int = 50) -> dict:
    """Capture the last N lines of a task's tmux pane."""
    session = spawner.tmux_session_name(task_id)
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="timeout capturing pane")
    if result.returncode != 0:
        raise HTTPException(status_code=404, detail=f"tmux session '{session}' not found")
    output_lines = result.stdout.strip().split("\n")
    return {"task_id": task_id, "output": "\n".join(output_lines[-lines:])}


# --- Write endpoints (phase 1 — kind=task only; kind=service still on systemd) ---


@app.post("/tasks/{task_id}/spawn")
def spawn(task_id: str) -> dict:
    """Spawn a task in tmux. Phase 1 supports kind=task only; service-kind tasks
    continue to be created by server.py via the per-task systemd unit until
    phase 2 migrates them under the daemon."""
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    if task["status"] == "running":
        conn.close()
        raise HTTPException(status_code=409, detail=f"task '{task_id}' is already running")

    kind = task.get("kind", "task")
    if kind == "service":
        conn.close()
        raise HTTPException(
            status_code=501,
            detail="kind=service spawn via daemon: phase 2 not yet implemented",
        )

    plugins = json.loads(task["plugins"]) if task["plugins"] else []
    model = task.get("model")
    cwd = task.get("cwd")
    channels = json.loads(task["channels"]) if task.get("channels") else []

    try:
        success = spawner.spawn_tmux(
            task_id, plugins, model=model, cwd=cwd, channels=channels, kind=kind,
        )
    except spawner.ChannelResolutionError as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"channel validation failed: {e}")

    if not success:
        conn.close()
        raise HTTPException(
            status_code=502,
            detail="spawn failed: tmux launched but channel never registered with session-bridge within 20s",
        )

    store.update_status(conn, task_id, "running")
    store.increment_invocation(conn, task_id)
    conn.close()

    spawner.send_initial_prompt(task_id, task["description"])

    return {
        "status": "running",
        "task_id": task_id,
        "kind": "task",
        "tmux_session": spawner.tmux_session_name(task_id),
        "channel_healthy": True,
    }


@app.post("/tasks/{task_id}/kill")
def kill(task_id: str) -> dict:
    """Kill a running task. Phase 1: kind=task only."""
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")

    kind = task.get("kind", "task")
    if kind == "service":
        conn.close()
        raise HTTPException(
            status_code=501,
            detail="kind=service kill via daemon: phase 2 not yet implemented",
        )

    tmux_killed = spawner.kill_tmux(task_id)
    spawner.cleanup_project_mcps(task_id)
    store.update_status(conn, task_id, "killed")
    conn.close()

    return {
        "task_id": task_id,
        "status": "killed",
        "tmux_killed": tmux_killed,
    }


@app.post("/tasks/{task_id}/message")
def message(task_id: str, body: MessageRequest) -> dict:
    """Forward a message to a task via session-bridge. Works for any kind."""
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()
    if not task:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")

    if not spawner.channel_healthy(task_id):
        raise HTTPException(
            status_code=502,
            detail=f"task '{task_id}' channel not reachable via session-bridge",
        )

    payload = json.dumps({
        "text": body.text,
        "from_session": body.from_session or "taskpilot-daemon",
    })
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
             "-d", payload, f"{spawner.SESSION_BRIDGE_URL}/sessions/{task_id}/message"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="timeout sending message")

    return {"delivered": result.returncode == 0, "response": result.stdout}


# --- Entry point ---


def main() -> None:
    port = int(os.environ.get("TASKPILOT_DAEMON_PORT", DEFAULT_PORT))
    bind = os.environ.get("TASKPILOT_DAEMON_BIND", "127.0.0.1")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    uvicorn.run(app, host=bind, port=port, log_level="info")


if __name__ == "__main__":
    main()
