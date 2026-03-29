"""MCP server for taskpilot — task lifecycle and messaging."""

import json
import subprocess
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
) -> dict:
    """Create a new autonomous task. Writes config files and allocates a channel port.

    Args:
        name: Human-readable task name (e.g., "Sell my lawnmower").
        description: Full task description — what the agent should do.
        plugins: Optional list of plugin directory paths to load.

    Returns:
        Task record with task_id, port, and status.
    """
    task_id = spawner.slugify(name)
    plugins = plugins or []

    conn = store.get_db()

    # Check for duplicate
    existing = store.get_task(conn, task_id)
    if existing:
        conn.close()
        return {"error": f"Task '{task_id}' already exists with status '{existing['status']}'"}

    task = store.create_task(conn, task_id, name, description, plugins)
    conn.close()

    # Write config files
    spawner.write_task_config(task_id, name, description, plugins)

    # Register channel MCP in .claude.json
    spawner.register_channel_mcp(task_id, task["port"])

    return task


@mcp.tool()
def spawn_task(task_id: str) -> dict:
    """Launch a created task in a tmux session with its channel.

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

    # Launch tmux session (blocks ~16s for startup dialogs)
    success = spawner.spawn_tmux(task_id, port, plugins)
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

    # Kill tmux
    tmux_killed = spawner.kill_tmux(task_id)

    # Unregister channel MCP
    spawner.unregister_channel_mcp(task_id)

    # Update DB
    store.update_status(conn, task_id, "killed")
    conn.close()

    return {
        "task_id": task_id,
        "status": "killed",
        "tmux_killed": tmux_killed,
    }


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


if __name__ == "__main__":
    mcp.run()
