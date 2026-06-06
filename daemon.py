#!/usr/bin/env python3
"""Taskpilot supervisor daemon.

A long-lived local service that owns the spawn/kill/message lifecycle for
tasks and exposes it over an HTTP API on :8912. The MCP server (server.py)
is a thin client over this daemon.

The daemon installs as a boot-persistence service (systemd user unit on
Linux, launchd agent on macOS) via `daemon.py --install`. It is reactive:
it acts on API calls, not on a background timer. Liveness (is the agent's
tmux still alive) is reported on-demand when a task is listed or fetched.
"""

import json
import logging
import os
import platform
import subprocess
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


# --- Boot-persistence service installation (systemd on Linux, launchd on macOS) ---


SYSTEMD_UNIT_NAME = "taskpilot-daemon.service"
SYSTEMD_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME

LAUNCHD_LABEL = "com.softwaresoftware.taskpilot-daemon"
LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _resolve_uv() -> str:
    """Find an absolute path to the `uv` binary, falling back to bare 'uv'.

    The daemon's plugin deps (mcp, fastapi, uvicorn) live in the plugin's uv
    venv, not in system python. `uv run --directory <plugin>` is the only
    invocation that finds the right interpreter on every host.
    """
    found = subprocess.run(["which", "uv"], capture_output=True, text=True).stdout.strip()
    return found or "uv"


def _systemd_unit_text() -> str:
    """Render the taskpilot-daemon.service unit file.

    Hardcoded paths are intentional — systemd resolves nothing from PATH and
    refuses to substitute env vars in ExecStart. The exempt-for-local-config
    carveout in the projects CLAUDE.md applies.
    """
    uv = _resolve_uv()
    plugin_root = str(Path(__file__).resolve().parent)
    return f"""[Unit]
Description=Taskpilot supervisor daemon
Documentation=https://github.com/softwaresoftware-dev/taskpilot
After=network.target session-bridge.service
Wants=session-bridge.service

[Service]
Type=simple
ExecStart={uv} run --directory {plugin_root} python daemon.py
Restart=on-failure
RestartSec=5
# Only kill the daemon's main process on stop, not its descendants. The
# daemon spawns detached tmux sessions for each task; the default
# control-group KillMode would tear those down on every daemon restart,
# orphaning every running agent.
KillMode=process
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


def _launchd_plist_text() -> str:
    """Render the taskpilot-daemon launchd agent plist (macOS).

    AbandonProcessGroup mirrors the systemd unit's `KillMode=process`: the
    daemon spawns detached tmux sessions per task, and launchd must not tear
    those down when it stops/restarts the daemon. KeepAlive.SuccessfulExit=false
    mirrors `Restart=on-failure`.
    """
    uv = _resolve_uv()
    plugin_root = str(Path(__file__).resolve().parent)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{uv}</string>
        <string>run</string>
        <string>--directory</string>
        <string>{plugin_root}</string>
        <string>python</string>
        <string>daemon.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>AbandonProcessGroup</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/taskpilot-daemon.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/taskpilot-daemon.log</string>
</dict>
</plist>
"""


def install_launchd_agent() -> None:
    """Write the plist, (re)load it. Idempotent."""
    LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST_PATH.write_text(_launchd_plist_text())
    print(f"wrote {LAUNCHD_PLIST_PATH}")
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST_PATH)], check=False,
                    capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(LAUNCHD_PLIST_PATH)], check=True)
    print(f"loaded {LAUNCHD_LABEL}")


def uninstall_launchd_agent() -> None:
    """Unload and remove the plist. Idempotent."""
    subprocess.run(["launchctl", "unload", "-w", str(LAUNCHD_PLIST_PATH)], check=False,
                    capture_output=True)
    if LAUNCHD_PLIST_PATH.exists():
        LAUNCHD_PLIST_PATH.unlink()
        print(f"removed {LAUNCHD_PLIST_PATH}")
    print(f"uninstalled {LAUNCHD_LABEL}")


def install_daemon_service() -> None:
    """Install the boot-persistence service for the current OS."""
    osname = platform.system()
    if osname == "Linux":
        install_systemd_unit()
    elif osname == "Darwin":
        install_launchd_agent()
    else:
        print(f"taskpilot: no boot-persistence backend for {osname!r} — "
              "the daemon can still be run directly (python3 daemon.py).",
              file=sys.stderr)
        sys.exit(1)


def uninstall_daemon_service() -> None:
    """Uninstall the boot-persistence service for the current OS."""
    osname = platform.system()
    if osname == "Linux":
        uninstall_systemd_unit()
    elif osname == "Darwin":
        uninstall_launchd_agent()
    else:
        print(f"taskpilot: nothing to uninstall on {osname!r}.", file=sys.stderr)


# --- Entry point ---


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install_daemon_service()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        uninstall_daemon_service()
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
