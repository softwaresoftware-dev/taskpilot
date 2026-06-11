"""MCP server for taskpilot — task lifecycle and messaging.

A pure client over the taskpilot supervisor daemon (daemon.py). Every tool —
including define_task, which used to write the DB in-process — goes through
the daemon's HTTP API. The daemon is the service that owns running agents, so
it must be up; if it's unreachable the tools return a clear error rather than
silently doing the work in-process.
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

import spawner

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
            "Install/repair it with /taskpilot:setup (or run `python daemon.py` for dev)."
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
    """Define (or redefine) an autonomous task. Idempotent — defining an
    existing task updates its definition in place; the new definition applies
    from the next spawn.

    This only defines the task — call spawn_task(task_id) to launch it.

    Args:
        name: Human-readable task name (e.g., "Sell my lawnmower"). The
            task_id is this name slugified.
        description: Full task description — what the agent should do. Also
            the default starter prompt sent when the task spawns.
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
                agent's CLAUDE.md.
        model: Optional Claude model to use (e.g., "sonnet", "opus", "haiku").
        cwd: Optional working directory for the task (default: ~/.taskpilot/<task_id>/).

    Returns:
        Task record with task_id and a `created` flag (false = updated).
    """
    task_id = spawner.slugify(name)
    return _daemon_call("PUT", f"/tasks/{task_id}", json_body={
        "name": name,
        "description": description,
        "plugins": plugins,
        "brief": operating_brief,
        "model": model,
        "cwd": cwd,
    })


@mcp.tool()
def spawn_task(task_id: str, prompt: str | None = None) -> dict:
    """Ensure a task's agent is running (~16s startup when it actually
    spawns). Idempotent: a task that's already running is a no-op; a crashed
    or stopped one is respawned.

    Args:
        task_id: The task ID returned by define_task.
        prompt: Optional starter prompt override for this spawn only. By
            default the task's stored description is sent. Pass a
            resume-flavored prompt when reviving an agent that already has
            work in progress.

    Returns:
        {status, started, already_running, prompt_delivered?, ...}.
    """
    body = {"prompt": prompt} if prompt is not None else {}
    return _daemon_call("POST", f"/tasks/{task_id}/start", json_body=body)


@mcp.tool()
def list_tasks(status: str | None = None) -> list[dict] | dict:
    """List all tasks with reconciled status and live health.

    Args:
        status: Filter by status (defined/running/crashed/stopped/completed).
            None for all. Statuses are reconciled against tmux ground truth
            before filtering, so 'running' means actually running.

    Returns:
        List of task records with tmux_alive/channel_healthy.
    """
    qs = f"?status={status}" if status else ""
    return _daemon_call("GET", f"/tasks{qs}")


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Get full task detail including current state.json.

    Args:
        task_id: The task ID.

    Returns:
        Task record with reconciled status, live health, and state.json.
    """
    return _daemon_call("GET", f"/tasks/{task_id}")


@mcp.tool()
def send_message(task_id: str, message: str) -> dict:
    """Send a message to a running task, with verified delivery.

    Errors are actionable: `agent_not_running` means the agent is dead —
    spawn_task(task_id) revives it (pass a resume-flavored prompt), then
    resend. `channel_not_ready` means it's still booting — retry shortly.

    Args:
        task_id: The task ID.
        message: The message to send.

    Returns:
        {delivered: true} on success, {"error": {code, message}} otherwise.
    """
    return _daemon_call(
        "POST", f"/tasks/{task_id}/message",
        json_body={"text": message, "from_session": "taskpilot-mcp"},
    )


@mcp.tool()
def kill_task(task_id: str) -> dict:
    """Stop a task's agent — kills the tmux session and cleans up project
    MCPs. Idempotent: stopping an already-dead task is a no-op. The task row
    survives, so spawn_task(task_id) can relaunch it later.

    Args:
        task_id: The task ID.

    Returns:
        {status, tmux_killed}.
    """
    return _daemon_call("POST", f"/tasks/{task_id}/stop")


@mcp.tool()
def delete_task(task_id: str) -> dict:
    """Stop a task and delete it entirely — DB row and config dir — freeing
    the task id for reuse. Idempotent: deleting an absent task is a no-op.

    Args:
        task_id: The task ID.

    Returns:
        {deleted, existed}.
    """
    return _daemon_call("DELETE", f"/tasks/{task_id}")


if __name__ == "__main__":
    mcp.run()
