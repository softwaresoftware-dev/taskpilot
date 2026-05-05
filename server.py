"""MCP server for taskpilot — task lifecycle, messaging, and scheduling.

The MCP server is a thin client over the taskpilot supervisor daemon for
spawn/kill/message. Falls back to direct (in-process) calls if the daemon
isn't running, so tests and pre-daemon installs keep working. Phase 3
cleanup will remove the fallback once the daemon is mandatory.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import store
import spawner

mcp = FastMCP("taskpilot")

TASKPILOT_DIR = Path.home() / ".taskpilot"
SCHEDULES_FILE = TASKPILOT_DIR / "schedules.json"
DAEMON_URL = os.environ.get("TASKPILOT_DAEMON_URL", "http://127.0.0.1:8912")


def _daemon_call(method: str, path: str, json_body: dict | None = None) -> dict | None:
    """Call the taskpilot supervisor daemon over HTTP.

    Returns:
      dict with the daemon's JSON response on 2xx.
      dict {"error": "..."} on non-2xx (caller surfaces to user).
      None if the daemon is unreachable (caller falls back to direct call).
    """
    url = f"{DAEMON_URL}{path}"
    data = json.dumps(json_body).encode() if json_body else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return {"error": body.get("detail", str(e))}
        except Exception:
            return {"error": f"daemon returned {e.code}"}
    except urllib.error.URLError:
        return None  # daemon down — caller falls back


@mcp.tool()
def create_task(
    name: str,
    description: str,
    plugins: list[str] | None = None,
    operating_brief: dict | None = None,
    model: str | None = None,
    cwd: str | None = None,
    channels: list[str] | None = None,
    kind: str = "task",
    host: str | None = None,
) -> dict:
    """Create a new autonomous task. Writes config files and allocates a channel port.

    Args:
        name: Human-readable task name (e.g., "Sell my lawnmower").
        description: Full task description — what the agent should do.
        plugins: Optional list of plugin directory paths to load.
        operating_brief: Optional dict with richer task definition. Keys:
            objectives (list[str]): Measurable goals.
            workflows (list[str]): Ordered phases/steps.
            success_criteria (list[str]): How to know the task is done.
            boundaries (list[str]): What NOT to do.
            capabilities (list[str]): Required capabilities (e.g. ["memory", "scheduling"]).
            schedule (str): Cron expression for recurring agents.
        model: Optional Claude model to use (e.g., "sonnet", "opus", "haiku").
        cwd: Optional working directory for the task (default: ~/.taskpilot/<task_id>/).
        channels: Optional additional dev channel servers (e.g. ["server:session-bridge"]).
        kind: "task" for one-shot jobs, "service" for always-on agents that survive reboots.
        host: Optional mesh hostname to spawn on (e.g. "pixel-7-pro"). When set
            and not the local host, spawn_task forwards the launch to that
            peer's session-bridge daemon. None or self-host = local launch.
            kind="service" is not yet supported for remote hosts.

    Returns:
        Task record with task_id, port, and status.
    """
    if kind not in ("task", "service"):
        return {"error": f"Invalid kind '{kind}'. Must be 'task' or 'service'."}

    if host and kind == "service":
        return {"error": "kind=service is not yet supported on remote hosts (no remote systemd install)"}

    task_id = spawner.slugify(name)
    plugins = plugins or []
    operating_brief = operating_brief or {}
    channels = channels or []

    # Auto-resolve capability plugins via nov-dependency-resolver
    capabilities = operating_brief.get("capabilities", [])
    if capabilities:
        resolved = spawner.resolve_capabilities(capabilities)
        for path in resolved:
            if path not in plugins:
                plugins.append(path)

    conn = store.get_db()

    # Check for duplicate
    existing = store.get_task(conn, task_id)
    if existing:
        conn.close()
        return {"error": f"Task '{task_id}' already exists with status '{existing['status']}'"}

    task = store.create_task(conn, task_id, name, description, plugins, operating_brief, model, cwd, channels, kind=kind, host=host)
    conn.close()

    # Write config files
    spawner.write_task_config(task_id, name, description, plugins, operating_brief)

    return task


@mcp.tool()
def spawn_task(task_id: str) -> dict:
    """Launch a created task in a tmux session with its channel.

    For kind=service, installs a systemd user service that survives reboots.
    For kind=task (default), launches directly in tmux.

    Args:
        task_id: The task ID returned by create_task.

    Returns:
        Status of the spawn attempt.
    """
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        return {"error": f"Task '{task_id}' not found"}
    if task["status"] == "running":
        conn.close()
        return {"error": f"Task '{task_id}' is already running"}

    plugins = json.loads(task["plugins"]) if task["plugins"] else []
    model = task.get("model")
    cwd = task.get("cwd")
    channels = json.loads(task["channels"]) if task.get("channels") else []
    kind = task.get("kind", "task")
    host = task.get("host")

    # Remote host? Forward to that host's session-bridge /spawn. The peer's
    # daemon does the tmux + claude work and waits for registration.
    if host and not spawner.is_self_host(host):
        result = spawner.spawn_remote(task)
        if not result.get("spawned"):
            conn.close()
            return {"error": result.get("error", "remote spawn failed")}
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)
        conn.close()
        return {
            "status": "running",
            "task_id": task_id,
            "kind": "task",
            "host": host,
            "remote_session_id": result.get("session_id"),
            "tmux_session": result.get("tmux_session"),
            "channel_healthy": True,  # peer confirmed registration before returning
        }

    if kind == "service":
        # Generate startup script and install systemd service.
        # write_service_script validates channel refs upfront — surfaces a
        # clear error here instead of letting systemd respawn a broken
        # start.sh forever.
        try:
            spawner.write_service_script(task_id, plugins, model=model, cwd=cwd, channels=channels, kind=kind)
        except spawner.ChannelResolutionError as e:
            conn.close()
            return {"error": f"channel validation failed: {e}"}

        spawner.install_service(task_id)

        # Wait for systemd-launched start.sh to bring the channel up.
        channel_ready = spawner.wait_for_channel(task_id, timeout=40)

        # Update status — the service is "running" from systemd's perspective
        # even if the channel didn't register yet. The channel_ready bit in
        # the response tells the caller whether it's actually reachable.
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)
        conn.close()

        return {
            "status": "running",
            "task_id": task_id,
            "kind": "service",
            "tmux_session": spawner.tmux_session_name(task_id),
            "systemd_service": spawner._systemd_unit_name(task_id),
            "systemd_active": spawner.is_service_active(task_id),
            "channel_healthy": channel_ready,
        }
    else:
        # Standard task — try the supervisor daemon first (it owns spawn
        # going forward). If the daemon isn't running, fall back to the
        # in-process path so this MCP keeps working pre-daemon and in tests.
        conn.close()
        daemon_result = _daemon_call("POST", f"/tasks/{task_id}/spawn")
        if daemon_result is not None:
            return daemon_result

        # Daemon down — direct path.
        conn = store.get_db()
        try:
            success = spawner.spawn_tmux(task_id, plugins, model=model, cwd=cwd, channels=channels, kind=kind)
        except spawner.ChannelResolutionError as e:
            conn.close()
            return {"error": f"channel validation failed: {e}"}

        if not success:
            conn.close()
            return {"error": "spawn failed: tmux launched but channel never registered with session-bridge within 20s"}

        # Update status
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)
        conn.close()

        # Send initial task prompt via session-bridge
        spawner.send_initial_prompt(task_id, task["description"])

        return {
            "status": "running",
            "task_id": task_id,
            "kind": "task",
            "tmux_session": spawner.tmux_session_name(task_id),
            # spawn_tmux confirmed channel before returning success.
            "channel_healthy": True,
        }


@mcp.tool()
def list_tasks(status: str | None = None) -> list[dict]:
    """List all tasks, optionally filtered by status.

    Args:
        status: Filter by status (pending/running/paused/completed/killed). None for all.

    Returns:
        List of task records.
    """
    conn = store.get_db()
    tasks = store.list_tasks(conn, status)
    conn.close()

    # Enrich with live status
    for t in tasks:
        t["tmux_alive"] = spawner.is_tmux_alive(t["task_id"])
        t["channel_healthy"] = spawner.channel_healthy(t["task_id"])
        if t.get("kind") == "service":
            t["systemd_active"] = spawner.is_service_active(t["task_id"])
    return tasks


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Get full task detail including current state.json.

    Args:
        task_id: The task ID.

    Returns:
        Task record with state.json contents if available.
    """
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()
    if not task:
        return {"error": f"Task '{task_id}' not found"}

    task["tmux_alive"] = spawner.is_tmux_alive(task_id)
    task["channel_healthy"] = spawner.channel_healthy(task_id)
    if task.get("kind") == "service":
        task["systemd_active"] = spawner.is_service_active(task_id)

    # Read state.json if it exists
    state_file = spawner.task_dir(task_id) / "state.json"
    if state_file.exists():
        try:
            task["state"] = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            task["state"] = {"error": "malformed state.json"}
    else:
        task["state"] = None

    return task


@mcp.tool()
def send_message(task_id: str, message: str) -> dict:
    """Send a message to a running task via its channel.

    Args:
        task_id: The task ID.
        message: The message to send.

    Returns:
        Delivery status.
    """
    # Route through daemon when available; fall back to direct otherwise.
    daemon_result = _daemon_call(
        "POST", f"/tasks/{task_id}/message",
        json_body={"text": message, "from_session": "taskpilot-mcp"},
    )
    if daemon_result is not None:
        return daemon_result

    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()
    if not task:
        return {"error": f"Task '{task_id}' not found"}

    if not spawner.channel_healthy(task_id):
        return {"error": f"Task '{task_id}' is not reachable via session-bridge"}

    payload = json.dumps({"text": message, "from_session": "taskpilot-mcp"})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
             "-d", payload, f"{spawner.SESSION_BRIDGE_URL}/sessions/{task_id}/message"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {"delivered": result.returncode == 0, "response": result.stdout}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout sending message"}


@mcp.tool()
def kill_task(task_id: str) -> dict:
    """Kill a running task — stops tmux session and cleans up channel MCP.

    For kind=service, also stops and disables the systemd user service.

    Args:
        task_id: The task ID.

    Returns:
        Result of kill attempt.
    """
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        return {"error": f"Task '{task_id}' not found"}

    kind = task.get("kind", "task")

    # For kind=task, try daemon first (it owns the lifecycle going forward).
    # kind=service still uses systemd until phase 2.
    if kind == "task":
        conn.close()
        daemon_result = _daemon_call("POST", f"/tasks/{task_id}/kill")
        if daemon_result is not None:
            return daemon_result
        conn = store.get_db()  # daemon down — fall through to direct path

    service_removed = False

    # For services, stop and disable the systemd unit
    if kind == "service":
        service_removed = spawner.uninstall_service(task_id)

    # Kill tmux (safety net for services, primary for tasks)
    tmux_killed = spawner.kill_tmux(task_id)

    # Clean up project-scoped MCPs this task registered
    spawner.cleanup_project_mcps(task_id)

    # Update DB
    store.update_status(conn, task_id, "killed")
    conn.close()

    result = {
        "task_id": task_id,
        "status": "killed",
        "tmux_killed": tmux_killed,
    }
    if kind == "service":
        result["service_removed"] = service_removed

    return result


@mcp.tool()
def get_task_log(task_id: str, lines: int = 50) -> dict:
    """Read recent output from a task's tmux pane.

    Args:
        task_id: The task ID.
        lines: Number of lines to capture (default 50).

    Returns:
        Captured pane output.
    """
    session = spawner.tmux_session_name(task_id)
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {"error": f"Failed to capture pane: {result.stderr}"}
        output_lines = result.stdout.strip().split("\n")
        return {"output": "\n".join(output_lines[-lines:])}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout capturing pane"}


@mcp.tool()
def destroy_task(task_id: str) -> dict:
    """Permanently delete a killed/completed task — removes DB row and config directory.

    Only works on tasks with status 'killed' or 'completed'. Use kill_task first
    to stop a running task.

    Args:
        task_id: The task ID to destroy.

    Returns:
        Result of the destroy attempt.
    """
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        return {"error": f"Task '{task_id}' not found"}
    if task["status"] in ("running", "pending"):
        conn.close()
        return {"error": f"Task '{task_id}' is {task['status']} — kill it first"}

    # Remove config directory
    import shutil
    td = spawner.task_dir(task_id)
    config_removed = False
    if td.exists():
        shutil.rmtree(td)
        config_removed = True

    # Remove DB row
    store.delete_task(conn, task_id)
    conn.close()

    return {
        "task_id": task_id,
        "destroyed": True,
        "config_removed": config_removed,
    }


@mcp.tool()
def respawn_task(task_id: str) -> dict:
    """Respawn a killed task — resets status to pending and launches it again.

    Re-uses the original task config (description, plugins, model, kind).
    Increments invocation_count.

    Args:
        task_id: The task ID to respawn.

    Returns:
        Result of the respawn attempt (same as spawn_task).
    """
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    if not task:
        conn.close()
        return {"error": f"Task '{task_id}' not found"}
    if task["status"] == "running":
        conn.close()
        return {"error": f"Task '{task_id}' is already running"}

    # Reset status to pending so spawn_task accepts it
    store.update_status(conn, task_id, "pending")
    conn.close()

    # Delegate to spawn_task
    return spawn_task(task_id)


# ---------------------------------------------------------------------------
# Scheduling — cron-based recurring task events (merged from scheduler-cron)
# ---------------------------------------------------------------------------


def _read_schedules() -> dict:
    if not SCHEDULES_FILE.exists():
        return {}
    try:
        return json.loads(SCHEDULES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_schedules(schedules: dict) -> None:
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULES_FILE.write_text(json.dumps(schedules, indent=2))


def _get_current_crontab() -> str:
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _set_crontab(content: str) -> bool:
    try:
        result = subprocess.run(
            ["crontab", "-"], input=content, capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _schedule_tag(task_id: str, name: str) -> str:
    return f"# taskpilot-schedule:{task_id}:{name}"


def _human_to_cron(interval: str) -> str | None:
    """Convert human-readable intervals to cron expressions.

    Supports cron expressions (5 fields), "every Xm/Xh/Xd", "daily", "hourly", "weekly".
    """
    interval = interval.strip()
    parts = interval.split()
    if len(parts) == 5:
        return interval

    lower = interval.lower()
    if lower == "daily":
        return "0 9 * * *"
    if lower == "hourly":
        return "0 * * * *"
    if lower == "weekly":
        return "0 9 * * 1"

    if lower.startswith("every "):
        spec = lower[6:].strip()
        if spec.endswith("m"):
            try:
                return f"*/{int(spec[:-1])} * * * *"
            except ValueError:
                pass
        elif spec.endswith("h"):
            try:
                return f"0 */{int(spec[:-1])} * * *"
            except ValueError:
                pass
        elif spec.endswith("d"):
            try:
                return f"0 9 */{int(spec[:-1])} * *"
            except ValueError:
                pass
    return None


@mcp.tool()
def schedule_task(
    name: str,
    plugin: str,
    skill: str,
    interval: str,
    enabled: bool = True,
) -> dict:
    """Schedule a recurring task event via crontab.

    Creates a crontab entry that POSTs a message to the agent's session-bridge
    channel on the specified interval. The agent receives the message and
    decides what to do.

    Args:
        name: Unique name for this schedule (e.g., "daily-research", "price-check").
        plugin: Plugin name for context (included in the message).
        skill: Skill or workflow to trigger (included in the message).
        interval: Cron expression (5 fields) or human-readable ("every 30m", "daily", "hourly").
        enabled: Whether the schedule is active (default True).

    Returns:
        Confirmation with schedule details.
    """
    cron_expr = _human_to_cron(interval)
    if not cron_expr:
        return {"error": f"Invalid interval: '{interval}'. Use cron (5 fields), 'every Xm/Xh/Xd', 'daily', 'hourly', or 'weekly'."}

    task_id = os.environ.get("TASKPILOT_TASK_ID", "unknown")
    if task_id == "unknown":
        return {"error": "Cannot determine task id. Is TASKPILOT_TASK_ID set?"}

    tag = _schedule_tag(task_id, name)
    message = f"[scheduled:{name}] Time to run {skill} (plugin: {plugin})"
    payload = json.dumps({"text": message, "from_session": "cron"})
    target_url = f"{spawner.SESSION_BRIDGE_URL}/sessions/{task_id}/message"
    # Crontab-safe: single-quote the JSON body, escape inner single quotes.
    cron_line = (
        f"{cron_expr} curl -s -X POST -H 'Content-Type: application/json' "
        f"-d {json.dumps(payload)} {target_url} > /dev/null 2>&1 {tag}"
    )

    # Update crontab
    current = _get_current_crontab()
    lines = [l for l in current.splitlines() if tag not in l]
    if enabled:
        lines.append(cron_line)
    new_crontab = "\n".join(lines)
    if new_crontab and not new_crontab.endswith("\n"):
        new_crontab += "\n"
    if not _set_crontab(new_crontab):
        return {"error": "Failed to update crontab"}

    # Update schedules registry
    schedules = _read_schedules()
    schedules[f"{task_id}:{name}"] = {
        "name": name,
        "task_id": task_id,
        "plugin": plugin,
        "skill": skill,
        "interval": interval,
        "cron_expr": cron_expr,
        "target_url": target_url,
        "enabled": enabled,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_schedules(schedules)

    return {
        "scheduled": True,
        "name": name,
        "cron_expr": cron_expr,
        "target_url": target_url,
        "message": message,
        "enabled": enabled,
    }


@mcp.tool()
def list_scheduled_tasks() -> dict:
    """List all scheduled tasks for the current agent.

    Returns:
        List of active schedules with their details.
    """
    task_id = os.environ.get("TASKPILOT_TASK_ID", "unknown")
    schedules = _read_schedules()
    task_schedules = [s for s in schedules.values() if s.get("task_id") == task_id]
    return {"task_id": task_id, "count": len(task_schedules), "schedules": task_schedules}


@mcp.tool()
def remove_scheduled_task(name: str) -> dict:
    """Remove a scheduled task.

    Args:
        name: The schedule name to remove.

    Returns:
        Confirmation of removal.
    """
    task_id = os.environ.get("TASKPILOT_TASK_ID", "unknown")
    tag = _schedule_tag(task_id, name)

    # Remove from crontab
    current = _get_current_crontab()
    lines = [l for l in current.splitlines() if tag not in l]
    new_crontab = "\n".join(lines)
    if new_crontab and not new_crontab.endswith("\n"):
        new_crontab += "\n"
    _set_crontab(new_crontab)

    # Remove from schedules registry
    schedules = _read_schedules()
    key = f"{task_id}:{name}"
    removed = key in schedules
    schedules.pop(key, None)
    _write_schedules(schedules)

    return {"removed": removed, "name": name, "task_id": task_id}


if __name__ == "__main__":
    mcp.run()
