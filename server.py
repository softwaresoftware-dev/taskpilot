"""MCP server for taskpilot — task lifecycle and messaging.

A thin client over the taskpilot supervisor daemon (daemon.py). `define_task`
writes config + a DB row locally; every lifecycle call (spawn/kill/message,
plus the reads) goes through the daemon's HTTP API. The daemon is the service
that actually owns running agents, so it must be up — if it's unreachable the
tools return a clear error rather than silently doing the work in-process.
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

import spawner
import store

mcp = FastMCP("taskpilot")

DAEMON_URL = os.environ.get("TASKPILOT_DAEMON_URL", "http://127.0.0.1:8912")


def _daemon_call(method: str, path: str, json_body: dict | None = None) -> dict | list:
    """Call the taskpilot supervisor daemon over HTTP.

    Every response is a value the tool can return directly:
      dict/list — the daemon's JSON response on 2xx.
      {"error": ...} — on a non-2xx, or when the daemon is unreachable.
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
        return {
            "error": f"taskpilot daemon is not reachable at {DAEMON_URL}. "
            "Start it with `python daemon.py` (or install it: `python daemon.py --install`)."
        }


@mcp.tool()
def define_task(
    name: str,
    description: str,
    plugins: list[str] | None = None,
    operating_brief: dict | None = None,
    model: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Define a new autonomous task. Writes config files and allocates a channel port.

    This only defines the task — call spawn_task(task_id) to launch it.

    Args:
        name: Human-readable task name (e.g., "Sell my lawnmower").
        description: Full task description — what the agent should do.
        plugins: Optional list of plugin directory paths to load as dev-mode
            --plugin-dir flags. Only needed for plugins NOT already installed —
            the agent inherits the user's full ~/.claude (all installed plugins
            and MCP servers) automatically.
        operating_brief: Optional dict with richer task definition. Keys:
            objectives (list[str]): Measurable goals.
            workflows (list[str]): Ordered phases/steps.
            success_criteria (list[str]): How to know the task is done.
            boundaries (list[str]): What NOT to do.
            capabilities (list[str]): Capabilities to remind the agent it has
                (e.g. ["memory"]). These become guidance sections in the
                agent's CLAUDE.md — the tools themselves come from the inherited
                environment, so nothing is resolved or installed here.
        model: Optional Claude model to use (e.g., "sonnet", "opus", "haiku").
        cwd: Optional working directory for the task (default: ~/.taskpilot/<task_id>/).

    Returns:
        Task record with task_id, port, and status.
    """
    task_id = spawner.slugify(name)
    plugins = plugins or []
    operating_brief = operating_brief or {}

    with store.db() as conn:
        existing = store.get_task(conn, task_id)
        if existing:
            return {"error": f"Task '{task_id}' already exists with status '{existing['status']}'"}
        task = store.create_task(conn, task_id, name, description, plugins, operating_brief, model, cwd)

    spawner.write_task_config(task_id, name, description, plugins, operating_brief)

    return task


@mcp.tool()
def spawn_task(task_id: str) -> dict:
    """Launch a created task in a tmux session with its channel (~16s startup).

    Args:
        task_id: The task ID returned by define_task.

    Returns:
        Status of the spawn attempt.
    """
    return _daemon_call("POST", f"/tasks/{task_id}/spawn")


@mcp.tool()
def list_tasks(status: str | None = None) -> list[dict] | dict:
    """List all tasks, optionally filtered by status.

    Args:
        status: Filter by status (pending/running/killed). None for all.

    Returns:
        List of task records with live tmux/channel health.
    """
    qs = f"?status={status}" if status else ""
    return _daemon_call("GET", f"/tasks{qs}")


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Get full task detail including current state.json.

    Args:
        task_id: The task ID.

    Returns:
        Task record with state.json contents if available.
    """
    return _daemon_call("GET", f"/tasks/{task_id}")


@mcp.tool()
def send_message(task_id: str, message: str) -> dict:
    """Send a message to a running task via its channel.

    Args:
        task_id: The task ID.
        message: The message to send.

    Returns:
        Delivery status.
    """
    return _daemon_call(
        "POST", f"/tasks/{task_id}/message",
        json_body={"text": message, "from_session": "taskpilot-mcp"},
    )


@mcp.tool()
def kill_task(task_id: str) -> dict:
    """Kill a running task — stops the tmux session and cleans up channel MCPs.

    Args:
        task_id: The task ID.

    Returns:
        Result of kill attempt.
    """
    return _daemon_call("POST", f"/tasks/{task_id}/kill")


if __name__ == "__main__":
    mcp.run()
