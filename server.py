"""MCP server for taskpilot — task lifecycle and messaging."""

import json
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import store
import spawner

mcp = FastMCP("taskpilot")


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

    Returns:
        Task record with task_id, port, and status.
    """
    if kind not in ("task", "service"):
        return {"error": f"Invalid kind '{kind}'. Must be 'task' or 'service'."}

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

    task = store.create_task(conn, task_id, name, description, plugins, operating_brief, model, cwd, channels, kind=kind)
    conn.close()

    # Write config files
    spawner.write_task_config(task_id, name, description, plugins, operating_brief)

    # Register channel MCP in .claude.json (for kind=task; services handle this in start.sh)
    if kind == "task":
        spawner.register_channel_mcp(task_id, task["port"])

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
    port = task["port"]
    model = task.get("model")
    cwd = task.get("cwd")
    channels = json.loads(task["channels"]) if task.get("channels") else []
    kind = task.get("kind", "task")

    if kind == "service":
        # Generate startup script and install systemd service
        spawner.write_service_script(task_id, port, plugins, model=model, cwd=cwd, channels=channels)
        spawner.install_service(task_id)

        # Poll for channel health (systemd starts the script which starts tmux)
        for _ in range(40):
            if spawner.channel_healthy(port):
                break
            time.sleep(1)

        # Update status
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)
        conn.close()

        return {
            "status": "running",
            "task_id": task_id,
            "port": port,
            "kind": "service",
            "tmux_session": spawner.tmux_session_name(task_id),
            "systemd_service": spawner._systemd_unit_name(task_id),
            "systemd_active": spawner.is_service_active(task_id),
            "channel_healthy": spawner.channel_healthy(port),
        }
    else:
        # Standard task — launch directly in tmux
        success = spawner.spawn_tmux(task_id, port, plugins, model=model, cwd=cwd, channels=channels)
        if not success:
            conn.close()
            return {"error": "Failed to launch tmux session"}

        # Update status
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)
        conn.close()

        # Send initial task prompt
        spawner.send_initial_prompt(port, task["description"])

        return {
            "status": "running",
            "task_id": task_id,
            "port": port,
            "kind": "task",
            "tmux_session": spawner.tmux_session_name(task_id),
            "channel_healthy": spawner.channel_healthy(port),
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
        t["channel_healthy"] = spawner.channel_healthy(t["port"])
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
    task["channel_healthy"] = spawner.channel_healthy(task["port"])
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
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()
    if not task:
        return {"error": f"Task '{task_id}' not found"}

    port = task["port"]
    if not spawner.channel_healthy(port):
        return {"error": f"Channel on port {port} is not responding"}

    try:
        result = subprocess.run(
            ["curl", "-s", "-d", message, f"http://localhost:{port}"],
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
    service_removed = False

    # For services, stop and disable the systemd unit
    if kind == "service":
        service_removed = spawner.uninstall_service(task_id)

    # Kill tmux (safety net for services, primary for tasks)
    tmux_killed = spawner.kill_tmux(task_id)

    # Unregister channel MCP
    spawner.unregister_channel_mcp(task_id)

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


if __name__ == "__main__":
    mcp.run()
