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

import asyncio
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
RECONCILE_INTERVAL_S = int(os.environ.get("TASKPILOT_RECONCILE_INTERVAL_S", "60"))

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


# --- Reconciler ---
#
# Phase 2: every RECONCILE_INTERVAL_S seconds the daemon walks tasks with
# status='running' and reconciles DB ↔ reality:
#   - tmux alive → stamp last_seen_at (heartbeat)
#   - tmux dead, kind=service → respawn (the supervisor part)
#   - tmux dead, kind=task    → mark crashed (one-shot semantics)
#
# This single tick subsumes three concerns previously split across files:
#   • boot-time spawn-up of services (replaces per-task systemd units that
#     ran start.sh on boot)
#   • crash recovery (replaces the bash while-loop that respawned claude
#     when it exited inside its tmux session)
#   • liveness reconciliation (replaces the standalone liveness.py timer)


def _spawn_body(task: dict) -> None:
    """Synchronous spawn for one task. Used by /spawn endpoint and reconciler.

    Raises spawner.ChannelResolutionError on bad channels, RuntimeError on
    spawn failures (callers convert to HTTP errors or log).
    """
    task_id = task["task_id"]
    plugins = json.loads(task["plugins"]) if task["plugins"] else []
    model = task.get("model")
    cwd = task.get("cwd")
    channels = json.loads(task["channels"]) if task.get("channels") else []
    kind = task.get("kind", "task")

    success = spawner.spawn_tmux(
        task_id, plugins, model=model, cwd=cwd, channels=channels, kind=kind,
    )
    if not success:
        raise RuntimeError(
            f"spawn failed for {task_id}: tmux launched but channel never registered"
        )


def reconcile_once() -> dict:
    """One reconciler pass. Returns counts for logging/metrics.

    Read DB, check tmux for each running task, take action, write DB back.
    Synchronous so we can run it via asyncio.to_thread from the loop.
    """
    counts = {"checked": 0, "alive": 0, "respawned": 0, "crashed": 0, "failed": 0}
    conn = store.get_db()
    try:
        running = store.list_tasks(conn, "running")
    except Exception as e:
        conn.close()
        log.error("reconcile: failed to list tasks: %s", e)
        return counts

    for task in running:
        task_id = task["task_id"]
        kind = task.get("kind", "task")
        counts["checked"] += 1

        if spawner.is_tmux_alive(task_id):
            store.mark_seen(conn, task_id)
            counts["alive"] += 1
            continue

        # tmux died. Service-kind: try to bring it back. Task-kind: mark crashed.
        if kind == "service":
            log.warning("reconcile: tmux dead for service %s — respawning", task_id)
            try:
                _spawn_body(task)
                store.increment_invocation(conn, task_id)
                counts["respawned"] += 1
            except Exception as e:
                log.error("reconcile: respawn of %s failed: %s", task_id, e)
                store.mark_crashed(conn, task_id, f"reconciler respawn failed: {e}")
                counts["failed"] += 1
        else:
            log.info("reconcile: tmux dead for task %s — marking crashed", task_id)
            store.mark_crashed(conn, task_id, "tmux died (reconciler)")
            counts["crashed"] += 1

    conn.close()
    return counts


async def reconcile_loop() -> None:
    """Background async loop. Runs reconcile_once on RECONCILE_INTERVAL_S cadence.

    First pass fires immediately on daemon boot — that's what brings services
    back up after a host reboot. Sleep is at the *end* so a slow tick doesn't
    delay the next tick beyond the interval.
    """
    while True:
        try:
            counts = await asyncio.to_thread(reconcile_once)
            if any(counts[k] for k in ("respawned", "crashed", "failed")):
                log.info(
                    "reconcile: checked=%d alive=%d respawned=%d crashed=%d failed=%d",
                    counts["checked"], counts["alive"], counts["respawned"],
                    counts["crashed"], counts["failed"],
                )
        except Exception as e:
            log.exception("reconcile: unhandled error: %s", e)
        await asyncio.sleep(RECONCILE_INTERVAL_S)


# --- Lifespan ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("taskpilot daemon starting (reconcile interval=%ds)", RECONCILE_INTERVAL_S)
    task = asyncio.create_task(reconcile_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
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


def _enrich(task: dict) -> dict:
    """Add live health fields to a task row. Mutates and returns."""
    tid = task["task_id"]
    task["tmux_alive"] = spawner.is_tmux_alive(tid)
    task["channel_healthy"] = spawner.channel_healthy(tid)
    if task.get("kind") == "service":
        task["systemd_active"] = spawner.is_service_active(tid)
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
    conn = store.get_db()
    tasks = store.list_tasks(conn, status)
    conn.close()
    return [_enrich(t) for t in tasks]


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    """Full task detail with live health and state.json."""
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()
    if not task:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    _enrich(task)
    task["state"] = _read_state(task_id)
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


# --- Write endpoints ---
#
# As of phase 2, /spawn and /kill handle both kind=task and kind=service.
# The difference is reconciler treatment, not spawn-path: services auto-respawn
# on tmux death, tasks get marked crashed. Spawn itself is the same call into
# spawner.spawn_tmux for both — no per-task systemd, no start.sh, no while-loop
# (the daemon's reconciler is the supervisor now).


@app.post("/tasks/{task_id}/spawn")
def spawn(task_id: str) -> dict:
    """Spawn a task in tmux."""
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    if task["status"] == "running":
        conn.close()
        raise HTTPException(status_code=409, detail=f"task '{task_id}' is already running")

    try:
        _spawn_body(task)
    except spawner.ChannelResolutionError as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"channel validation failed: {e}")
    except RuntimeError as e:
        conn.close()
        raise HTTPException(status_code=502, detail=str(e))

    store.update_status(conn, task_id, "running")
    store.increment_invocation(conn, task_id)
    conn.close()

    spawner.send_initial_prompt(task_id, task["description"])

    return {
        "status": "running",
        "task_id": task_id,
        "kind": task.get("kind", "task"),
        "tmux_session": spawner.tmux_session_name(task_id),
        "channel_healthy": True,
    }


@app.post("/tasks/{task_id}/kill")
def kill(task_id: str) -> dict:
    """Kill a running task. Reconciler doesn't touch killed tasks (its filter
    is status='running'), so killing a service stops the auto-respawn cycle."""
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")

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


# --- Systemd user unit installation ---


SYSTEMD_UNIT_NAME = "taskpilot-daemon.service"
SYSTEMD_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _systemd_unit_text() -> str:
    """Render the taskpilot-daemon.service unit file.

    Hardcoded paths are intentional — systemd resolves nothing from PATH and
    refuses to substitute env vars in ExecStart. The exempt-for-local-config
    carveout in the projects CLAUDE.md applies.
    """
    py = subprocess.run(["which", "python3"], capture_output=True, text=True).stdout.strip() or "/usr/bin/python3"
    daemon_py = str(Path(__file__).resolve())
    return f"""[Unit]
Description=Taskpilot supervisor daemon
Documentation=https://github.com/softwaresoftware-dev/taskpilot
After=network.target session-bridge.service
Wants=session-bridge.service

[Service]
Type=simple
ExecStart={py} {daemon_py}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def install_systemd_unit() -> None:
    """Write the unit file, daemon-reload, enable, start. Idempotent."""
    SYSTEMD_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEMD_UNIT_PATH.write_text(_systemd_unit_text())
    print(f"wrote {SYSTEMD_UNIT_PATH}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME], check=True)
    subprocess.run(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME], check=True)
    print(f"enabled and started {SYSTEMD_UNIT_NAME}")


def uninstall_systemd_unit() -> None:
    """Stop, disable, remove unit file. Idempotent."""
    subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME], check=False)
    subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME], check=False)
    if SYSTEMD_UNIT_PATH.exists():
        SYSTEMD_UNIT_PATH.unlink()
        print(f"removed {SYSTEMD_UNIT_PATH}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"uninstalled {SYSTEMD_UNIT_NAME}")


# --- Entry point ---


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install_systemd_unit()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        uninstall_systemd_unit()
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
