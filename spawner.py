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

# Absolute node path — nvm isn't in MCP subprocess PATH, and /usr/bin/node
# is v12 which can't run ES modules with top-level await.
# Must resolve to a node >= 18.
_node = shutil.which("node")
if _node and os.path.realpath(_node).startswith("/usr"):
    # System node is too old, find nvm version
    _node = None
if not _node:
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        versions = sorted(nvm_dir.iterdir(), reverse=True)
        for v in versions:
            candidate = v / "bin" / "node"
            if candidate.exists():
                _node = str(candidate)
                break
NODE_BIN = _node or "/usr/bin/node"


def slugify(name: str) -> str:
    """Convert task name to a valid slug for tmux session and task_id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:50]


def task_dir(task_id: str) -> Path:
    return TASKPILOT_DIR / task_id


def write_task_config(
    task_id: str,
    name: str,
    description: str,
    plugins: list[str],
    operating_brief: dict | None = None,
) -> Path:
    """Write CLAUDE.md and brief.json to the task directory."""
    td = task_dir(task_id)
    td.mkdir(parents=True, exist_ok=True)
    brief_data = operating_brief or {}

    # CLAUDE.md — dynamically assembled from operating brief
    claude_md = td / "CLAUDE.md"
    claude_md.write_text(_build_claude_md(name, description, brief_data))

    # brief.json — frozen config
    brief = {
        "task_id": task_id,
        "name": name,
        "description": description,
        "plugins": plugins,
        "operating_brief": brief_data,
    }
    (td / "brief.json").write_text(json.dumps(brief, indent=2))

    return td


def _build_claude_md(name: str, description: str, brief: dict) -> str:
    """Assemble CLAUDE.md sections dynamically based on the operating brief."""
    sections = []

    # Header (always)
    sections.append(f"# Task: {name}")

    # Mission (always)
    sections.append(f"## Mission\n{description}")

    # Objectives (if provided)
    objectives = brief.get("objectives")
    if objectives:
        items = "\n".join(f"- {obj}" for obj in objectives)
        sections.append(f"## Objectives\n{items}")

    # Workflows (if provided)
    workflows = brief.get("workflows")
    if workflows:
        items = "\n".join(f"{i+1}. {step}" for i, step in enumerate(workflows))
        sections.append(f"## Workflows\n{items}")

    # Success criteria (if provided)
    success_criteria = brief.get("success_criteria")
    if success_criteria:
        items = "\n".join(f"- {sc}" for sc in success_criteria)
        sections.append(f"## Success Criteria\n{items}")

    # Boundaries (if provided)
    boundaries = brief.get("boundaries")
    if boundaries:
        items = "\n".join(f"- {b}" for b in boundaries)
        sections.append(f"## Boundaries\n{items}")

    # Autonomy Rules (always)
    sections.append("""## Autonomy Rules (yessir protocol)
- NEVER ask "shall I continue?", "would you like me to...", or any confirmation prompt. The answer is always yes. Just do it.
- NEVER pause to summarize what you're about to do and ask for approval. Act, then report.
- DO continue working through your pending tasks without stopping.
- DO escalate ONLY when you need information you don't have, or you're about to do something irreversible and high-stakes.""")

    # Escalation (always)
    sections.append("""## How to Escalate to Human
When you genuinely need human input:
1. Reply on the channel with your question clearly stated
2. Continue other pending work while waiting
3. The human's reply arrives as a channel message — resume the blocked task when it arrives
4. If no response after a long time, log the blocked decision in state.json and move on""")

    # State File (always)
    sections.append("""## State File
- state.json (in this directory) is for crash recovery
- Write to it after every major action so that if this session dies, the next one can continue
- Format: {"phase": "...", "summary": "...", "completed": [...], "pending": [...], "data": {...}}
- Write it as a handoff document: what's done, what's pending, any data the next session needs""")

    # Channel Communication (always)
    sections.append("""## Channel Communication
Messages arrive as <channel> notifications.
Use the `reply` tool to respond. Always include useful context in replies.""")

    # Memory instructions (if memory capability declared)
    capabilities = brief.get("capabilities", [])
    if "memory" in capabilities:
        sections.append("""## Memory
You have persistent memory tools available. Use them to store institutional knowledge
that should survive across sessions — insights, experiment results, market data, learned
patterns. This is NOT crash recovery (that's state.json). Memory is for accumulated
knowledge that makes you smarter over time.

- `store_memory(key, content)` — save knowledge by topic
- `recall_memory(key)` — retrieve by key
- `search_memory(query)` — find relevant memories
- `list_memories()` — see what you know

Store a memory after every significant discovery or decision.""")

    # Human-approval instructions (if capability declared)
    if "human-approval" in capabilities:
        sections.append("""## Human Approval
You have human-approval tools available. Before taking any high-stakes or irreversible
action (posting publicly, spending money, sending external communications), use
`request_approval(action, context)` and wait for confirmation before proceeding.

Check approval status with `check_approval(request_id)`. If approval times out,
skip the action and log it to state.json.""")

    # Scheduling instructions (if capability declared)
    if "scheduling" in capabilities:
        sections.append("""## Scheduling
You have scheduling tools available. Use them to set up recurring workflows that
should run on a cadence — daily research, periodic checks, content schedules.

- `schedule_task(name, plugin, skill, interval)` — create a recurring event
- `list_scheduled_tasks()` — see active schedules
- `remove_scheduled_task(name)` — cancel a schedule

Scheduled events arrive as channel messages. Process them when they arrive.""")

    # On Startup (always)
    sections.append("""## On Startup
If state.json exists, read it first to understand your previous progress, then continue with pending items.""")

    return "\n\n".join(sections) + "\n"


def resolve_capabilities(capabilities: list[str]) -> list[str]:
    """Resolve capability names to installed plugin paths via nov-dependency-resolver.

    Returns:
        List of plugin directory paths for providers that match the declared capabilities.
    """
    if not capabilities:
        return []

    # Import nov-dependency-resolver's resolver directly (same machine, avoid MCP overhead)
    nov_hub_path = Path(__file__).parent.parent / "nov-dependency-resolver"
    if not nov_hub_path.exists():
        return []

    import sys
    original_path = sys.path[:]
    sys.path.insert(0, str(nov_hub_path))
    try:
        import resolver as hub_resolver
        import registry as hub_registry

        resolved_plugins = []
        for cap in capabilities:
            providers = hub_resolver.resolve(cap)
            for provider in providers:
                if provider["match"] and provider["installed"]:
                    # Look up install path
                    installed = hub_registry.get_installed_plugins()
                    for key, entries in installed.items():
                        if key.split("@")[0] == provider["name"] and entries:
                            install_path = entries[0].get("installPath", "")
                            if install_path and install_path not in resolved_plugins:
                                resolved_plugins.append(install_path)
                    break  # Use first matched+installed provider
        return resolved_plugins
    except ImportError:
        return []
    finally:
        sys.path[:] = original_path


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
    # Export TASKPILOT_TASK_ID so capability plugins can scope their storage
    cmd = f"""export TASKPILOT_TASK_ID={task_id}
while true; do
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

    # Unregister from .claude.json now that the task session owns the MCP process.
    # This prevents other Claude sessions from stealing the channel.
    unregister_channel_mcp(task_id)

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
