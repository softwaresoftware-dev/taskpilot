"""Spawner — writes config files, launches tmux session.

Messaging goes through session-bridge (localhost:8910). Agents are
addressable by task_id because we export SESSION_NAME=<task_id>; the
session-bridge channel.mjs reads that env var and includes it in its
/register payload, which is how the daemon names the session.
"""

import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

TASKPILOT_DIR = Path.home() / ".taskpilot"
CLAUDE_JSON = Path.home() / ".claude.json"
SESSION_NAMESPACE = "taskpilot"

# URL of the local session-bridge daemon. Honors session-bridge's own
# SESSION_BRIDGE_PORT convention (default 8910); a full SESSION_BRIDGE_URL
# override wins outright. Loopback host — taskpilot always talks to the bridge
# on its own machine.
SESSION_BRIDGE_URL = os.environ.get("SESSION_BRIDGE_URL") or (
    f"http://127.0.0.1:{os.environ.get('SESSION_BRIDGE_PORT', '8910')}"
)


def slugify(name: str) -> str:
    """Convert task name to a valid slug for tmux session and task_id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:50]


def task_dir(task_id: str) -> Path:
    return TASKPILOT_DIR / task_id


def tmux_session_name(task_id: str) -> str:
    return task_id


def post_to_channel(task_id: str, text: str, from_session: str) -> bool:
    """POST a message to a task's session-bridge channel. Returns True on 2xx.

    The single path for delivering text to a running agent — used by both the
    initial-prompt send and the daemon's /message endpoint.
    """
    data = json.dumps({"text": text, "from_session": from_session}).encode()
    req = urllib.request.Request(
        f"{SESSION_BRIDGE_URL}/sessions/{task_id}/message",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def write_task_config(
    task_id: str,
    name: str,
    description: str,
    plugins: list[str],
    operating_brief: dict | None = None,
) -> Path:
    """Write CLAUDE.md, brief.json, and prompt.txt to the task directory."""
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

    # prompt.txt — initial task prompt
    (td / "prompt.txt").write_text(description)

    return td


def _build_claude_md(name: str, description: str, brief: dict) -> str:
    """Assemble CLAUDE.md sections dynamically based on the operating brief."""
    sections = []

    sections.append(f"# Task: {name}")
    sections.append(f"## Mission\n{description}")

    objectives = brief.get("objectives")
    if objectives:
        items = "\n".join(f"- {obj}" for obj in objectives)
        sections.append(f"## Objectives\n{items}")

    workflows = brief.get("workflows")
    if workflows:
        items = "\n".join(f"{i+1}. {step}" for i, step in enumerate(workflows))
        sections.append(f"## Workflows\n{items}")

    success_criteria = brief.get("success_criteria")
    if success_criteria:
        items = "\n".join(f"- {sc}" for sc in success_criteria)
        sections.append(f"## Success Criteria\n{items}")

    boundaries = brief.get("boundaries")
    if boundaries:
        items = "\n".join(f"- {b}" for b in boundaries)
        sections.append(f"## Boundaries\n{items}")

    sections.append("""## Autonomy Rules (yessir protocol)
- NEVER ask "shall I continue?", "would you like me to...", or any confirmation prompt. The answer is always yes. Just do it.
- NEVER pause to summarize what you're about to do and ask for approval. Act, then report.
- DO continue working through your pending tasks without stopping.
- DO escalate ONLY when you need information you don't have, or you're about to do something irreversible and high-stakes.""")

    sections.append("""## No Interactive Prompts — You Are Headless
There is NO human watching your terminal and NO keyboard attached to this
session. You run unattended in a background tmux session; your only link to a
human is the channel (see below).
- NEVER present an interactive question, menu, or multiple-choice selection
  (e.g. the AskUserQuestion tool). It renders a prompt nobody can answer, and
  you will hang there forever — the session is not interactive.
- NEVER wait at any prompt for keyboard input.
- If you need a decision from a human, you MUST ask it as a CHANNEL MESSAGE
  (see "How to Escalate to Human"), not as a terminal prompt. A channel message
  is the only kind of question that can actually reach someone and be answered.
- When in doubt, make a reasonable decision yourself, record it in state.json,
  and keep going. A wrong-but-recorded choice is recoverable; a silent hang is not.""")

    sections.append("""## How to Escalate to Human
When you genuinely need human input:
1. Post your question to the channel (the `reply` tool), clearly stated — NOT as a terminal prompt
2. Continue other pending work while waiting
3. The human's reply arrives as a channel message — resume the blocked task when it arrives""")

    sections.append("""## State File
- state.json (in this directory) is for crash recovery
- Write to it after every major action so that if this session dies, the next one can continue
- Format: {"phase": "...", "summary": "...", "completed": [...], "pending": [...], "data": {...}}
- Write it as a handoff document: what's done, what's pending, any data the next session needs""")

    sections.append("""## Channel Communication
Messages arrive as <channel> notifications.
Use the `reply` tool to respond. Always include useful context in replies.""")

    # Capability sections — describe intent only. Tool names live in the
    # MCP servers' own descriptions, which Claude Code auto-loads into the
    # agent's context.
    capabilities = brief.get("capabilities", [])

    if "memory" in capabilities:
        sections.append("""## Memory
Persistent memory is available for institutional knowledge that should survive
across sessions — insights, experiment results, market data, learned patterns.
This is NOT crash recovery (that's state.json). Store a memory after every
significant discovery or decision. Use an available skill or tool.""")

    if "human-approval" in capabilities:
        sections.append("""## Human Approval
Before any high-stakes or irreversible action — posting publicly, spending
money, sending external communications — request human approval and wait for
confirmation. If approval times out, skip the action and log it to state.json.
Use an available skill or tool.""")

    sections.append("""## On Startup
If state.json exists, read it first to understand your previous progress, then continue with pending items.""")

    return "\n\n".join(sections) + "\n"


def cleanup_project_mcps(task_id: str) -> None:
    """Remove any project-scoped MCPs this task registered into ~/.claude.json.

    Project MCPs are registered at startup from the task cwd's
    .claude/settings.json (names recorded in project_mcps.json). We
    remove them when the task is torn down.
    """
    pmcps_file = task_dir(task_id) / "project_mcps.json"
    if not pmcps_file.exists():
        return
    try:
        names = json.loads(pmcps_file.read_text())
    except Exception:
        return
    if not names:
        return
    data = json.loads(CLAUDE_JSON.read_text())
    mcps = data.get("mcpServers", {})
    for name in names:
        mcps.pop(name, None)
    CLAUDE_JSON.write_text(json.dumps(data, indent=2))


def spawn_tmux(task_id: str, plugins: list[str], model: str | None = None,
               cwd: str | None = None) -> bool:
    """Launch the Claude session in tmux. Messaging goes through session-bridge.

    The agent inherits the user's real ~/.claude environment (global CLAUDE.md,
    rules, installed plugins, registered MCP servers).
    """
    session = tmux_session_name(task_id)
    # Default cwd is the task dir; an explicit cwd points the agent at a real
    # project instead.
    td = cwd or str(task_dir(task_id))

    # Build plugin-dir flags
    plugin_flags = ""
    for p in plugins:
        plugin_flags += f" --plugin-dir {p}"

    model_flag = f" --model {model}" if model else ""

    # session-bridge is the agent's channel — the messaging backbone. Loaded as
    # plugin:session-bridge@softwaresoftware-plugins (marketplace form); the
    # dangerously-load flag bypasses the channel allowlist for inbound
    # notifications. It's a hard dependency, so it's always installed.
    channels_arg = "plugin:session-bridge@softwaresoftware-plugins"

    # Env exports:
    #   TASKPILOT_TASK_ID — for capability plugins that scope storage per task
    #   SESSION_NAME      — read by session-bridge channel.mjs at /register
    #   SESSION_NAMESPACE — same
    #   CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false — no human is at the keyboard
    #     in a spawned agent, so the forked-suggestion LLM call is pure waste.
    cmd = f"""export TASKPILOT_TASK_ID={task_id}
export SESSION_NAME={task_id}
export SESSION_NAMESPACE={SESSION_NAMESPACE}
export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false
cd {td} && claude --dangerously-skip-permissions \\
  --dangerously-load-development-channels {channels_arg} \\
  {plugin_flags}{model_flag} \\
  --name {task_id}"""

    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, f"bash -lc '{cmd}'"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    # Auto-accept trust dialog (option 1, "Yes, I trust this folder", is default).
    time.sleep(7)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])

    # Auto-accept channels warning (default option is fine here)
    time.sleep(4)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])

    # Best-effort wait for session-bridge to register the channel by name.
    # Channel registration is eventually-consistent: channel.mjs re-registers
    # on its heartbeat whenever the bridge becomes reachable. A slow or
    # momentarily-unreachable bridge is NOT a spawn failure — the tmux+claude
    # process is up, and the channel converges on its own. A successful tmux
    # launch == spawned.
    if wait_for_channel(task_id, timeout=20):
        time.sleep(3)
    else:
        log.warning(
            "spawn %s: tmux up but channel not registered within 20s; "
            "proceeding anyway — channel.mjs will re-register on its heartbeat "
            "once session-bridge is reachable",
            task_id,
        )
    return True


def send_initial_prompt(task_id: str, description: str) -> bool:
    """POST the initial task prompt via session-bridge."""
    return post_to_channel(task_id, description, "taskpilot-spawner")


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


def channel_healthy(task_id: str) -> bool:
    """Check if session-bridge has a registered channel for this task."""
    req = urllib.request.Request(f"{SESSION_BRIDGE_URL}/sessions/{task_id}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        return data.get("channel_port") is not None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return False


def wait_for_channel(task_id: str, timeout: int = 20) -> bool:
    """Poll until the task's channel registers with session-bridge, or timeout.

    Returns True if the channel was healthy within the timeout, False otherwise.
    """
    for _ in range(timeout):
        if channel_healthy(task_id):
            return True
        time.sleep(1)
    return False
