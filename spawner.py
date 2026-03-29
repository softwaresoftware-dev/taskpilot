"""Spawner — writes config files, registers channel MCP, launches tmux session."""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

TASKPILOT_DIR = Path.home() / ".taskpilot"
CLAUDE_JSON = Path.home() / ".claude.json"
PLUGIN_ROOT = Path(__file__).parent
CHANNEL_TEMPLATE = PLUGIN_ROOT / "channel_template.mjs"

# Absolute node path — nvm isn't in MCP subprocess PATH
NODE_BIN = shutil.which("node") or "/home/thatcher/.nvm/versions/node/v22.12.0/bin/node"


def slugify(name: str) -> str:
    """Convert task name to a valid slug for tmux session and task_id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:50]


def task_dir(task_id: str) -> Path:
    return TASKPILOT_DIR / task_id


def write_task_config(task_id: str, name: str, description: str, plugins: list[str]) -> Path:
    """Write CLAUDE.md and brief.json to the task directory."""
    td = task_dir(task_id)
    td.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md — the agent's operating instructions
    claude_md = td / "CLAUDE.md"
    claude_md.write_text(f"""# Task: {name}

## Mission
{description}

## Autonomy Rules (yessir protocol)
- NEVER ask "shall I continue?", "would you like me to...", or any confirmation prompt. The answer is always yes. Just do it.
- NEVER pause to summarize what you're about to do and ask for approval. Act, then report.
- DO continue working through your pending tasks without stopping.
- DO escalate ONLY when you need information you don't have, or you're about to do something irreversible and high-stakes.

## How to Escalate to Human
When you genuinely need human input:
1. Reply on the channel with your question clearly stated
2. Continue other pending work while waiting
3. The human's reply arrives as a channel message — resume the blocked task when it arrives
4. If no response after a long time, log the blocked decision in state.json and move on

## State File
- state.json (in this directory) is for crash recovery
- Write to it after every major action so that if this session dies, the next one can continue
- Format: {{"phase": "...", "summary": "...", "completed": [...], "pending": [...], "data": {{...}}}}
- Write it as a handoff document: what's done, what's pending, any data the next session needs

## Channel Communication
Messages arrive as <channel> notifications.
Use the `reply` tool to respond. Always include useful context in replies.

## On Startup
If state.json exists, read it first to understand your previous progress, then continue with pending items.
""")

    # brief.json — frozen config
    brief = {
        "task_id": task_id,
        "name": name,
        "description": description,
        "plugins": plugins,
    }
    (td / "brief.json").write_text(json.dumps(brief, indent=2))

    return td


def register_channel_mcp(task_id: str, port: int) -> None:
    """Add the task's channel MCP server to ~/.claude.json."""
    server_name = f"task-{task_id}"
    channel_path = str(CHANNEL_TEMPLATE)

    data = json.loads(CLAUDE_JSON.read_text())
    data.setdefault("mcpServers", {})
    data["mcpServers"][server_name] = {
        "command": NODE_BIN,
        "args": [channel_path],
        "env": {
            "TASKPILOT_PORT": str(port),
            "TASKPILOT_NAME": server_name,
        },
    }
    CLAUDE_JSON.write_text(json.dumps(data, indent=2))


def unregister_channel_mcp(task_id: str) -> None:
    """Remove the task's channel MCP server from ~/.claude.json."""
    server_name = f"task-{task_id}"
    data = json.loads(CLAUDE_JSON.read_text())
    data.get("mcpServers", {}).pop(server_name, None)
    CLAUDE_JSON.write_text(json.dumps(data, indent=2))


def tmux_session_name(task_id: str) -> str:
    return f"taskpilot-{task_id}"


def spawn_tmux(task_id: str, port: int, plugins: list[str]) -> bool:
    """Launch the Claude session in tmux with channel."""
    session = tmux_session_name(task_id)
    server_name = f"task-{task_id}"
    td = task_dir(task_id)

    # Build plugin-dir flags
    plugin_flags = ""
    for p in plugins:
        plugin_flags += f" --plugin-dir {p}"

    # The tmux command: while-loop for crash recovery
    # rotation.py handles respawn decisions
    cmd = f"""while true; do
  cd {td} && \\
  claude --dangerously-skip-permissions \\
    --dangerously-load-development-channels server:{server_name} \\
    {plugin_flags} \\
    --name {task_id}
  python {PLUGIN_ROOT / 'rotation.py'} {task_id} || break
  sleep 5
done"""

    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, f"bash -lc '{cmd}'"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    # Auto-accept trust dialog
    time.sleep(7)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])

    # Auto-accept channels warning
    time.sleep(4)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])

    # Wait for channel health
    for _ in range(20):
        try:
            resp = subprocess.run(
                ["curl", "-sf", f"http://localhost:{port}/health"],
                capture_output=True,
                timeout=3,
            )
            if resp.returncode == 0:
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)

    # Brief settle time for MCP connection
    time.sleep(3)
    return True


def send_initial_prompt(port: int, description: str) -> bool:
    """POST the initial task prompt to the channel."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-d", description, f"http://localhost:{port}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def kill_tmux(task_id: str) -> bool:
    """Kill the tmux session for a task."""
    session = tmux_session_name(task_id)
    result = subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def is_tmux_alive(task_id: str) -> bool:
    """Check if the tmux session is running."""
    session = tmux_session_name(task_id)
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def channel_healthy(port: int) -> bool:
    """Check if the channel HTTP server is responding."""
    try:
        result = subprocess.run(
            ["curl", "-sf", f"http://localhost:{port}/health"],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
