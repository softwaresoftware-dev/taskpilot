"""Spawner — writes config files, launches tmux session.

Messaging goes through session-bridge (localhost:8910). Agents are
addressable by task_id because we export SESSION_NAME=<task_id>; the
session-bridge channel.mjs reads that env var and includes it in its
/register payload, which is how the daemon names the session.
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

TASKPILOT_DIR = Path.home() / ".taskpilot"
CLAUDE_JSON = Path.home() / ".claude.json"
PLUGIN_ROOT = Path(__file__).parent
SESSION_BRIDGE_URL = "http://127.0.0.1:8910"

# User-scope plugin registry — taskpilot only uses what's already installed.
INSTALLED_PLUGINS_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


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

    # prompt.txt — initial task prompt for service startup scripts
    (td / "prompt.txt").write_text(description)

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

    # Scheduling instructions (always available — built into taskpilot)
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


def _read_json(path: Path) -> dict | None:
    """Read a JSON file, return None if missing or invalid."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def resolve_capabilities(capabilities: list[str]) -> list[str]:
    """Resolve capability names to installed plugin directory paths.

    Walks ~/.claude/plugins/installed_plugins.json. For each capability,
    returns the path of an installed plugin whose manifest declares the
    capability in `provides`. Tie-breaker when multiple plugins satisfy
    the same capability: alphabetical by plugin name.

    Capabilities with no installed provider are silently skipped — the
    agent runs without them. Caller can compare returned paths against
    requested capabilities to detect this if it matters.
    """
    if not capabilities:
        return []

    installed = _read_json(INSTALLED_PLUGINS_PATH)
    if not installed:
        return []

    # Walk every installed plugin once, collect (name, path, provides) tuples.
    candidates = []
    for key, entries in installed.get("plugins", {}).items():
        if not entries:
            continue
        name = key.split("@")[0]
        path = entries[0].get("installPath")
        if not path or not Path(path).exists():
            continue
        manifest = _read_json(Path(path) / ".claude-plugin" / "plugin.json")
        provides = (manifest or {}).get("provides") or []
        if provides:
            candidates.append((name, path, set(provides)))

    candidates.sort(key=lambda c: c[0])

    resolved = []
    seen = set()
    for cap in capabilities:
        for name, path, provides in candidates:
            if cap in provides and path not in seen:
                resolved.append(path)
                seen.add(path)
                break

    return resolved


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


def tmux_session_name(task_id: str) -> str:
    return task_id


SESSION_NAMESPACE = "taskpilot"

HOOKS_DIR = PLUGIN_ROOT / "hooks"


def _session_labels(kind: str) -> str:
    """Comma-separated labels for SESSION_LABELS env var."""
    return f"kind:{kind}"


def write_hook_settings(task_id: str) -> Path:
    """Write a per-task settings file that registers Stop, Notification, and
    UserPromptSubmit hooks.

    Loaded by claude via `--settings <path>`. The flagSettings source merges
    with user/project settings rather than replacing them, so we add hooks
    without clobbering any existing config.
    """
    td = task_dir(task_id)
    td.mkdir(parents=True, exist_ok=True)

    on_stop = HOOKS_DIR / "on-stop.py"
    on_notification = HOOKS_DIR / "on-notification.py"
    on_prompt = HOOKS_DIR / "on-prompt.py"

    settings = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": str(on_stop)}]}],
            "Notification": [{"hooks": [{"type": "command", "command": str(on_notification)}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": str(on_prompt)}]}],
        }
    }

    path = td / "hook-settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path


def spawn_tmux(task_id: str, plugins: list[str], model: str | None = None,
               cwd: str | None = None, channels: list[str] | None = None,
               kind: str = "task") -> bool:
    """Launch the Claude session in tmux. Messaging goes through session-bridge."""
    session = tmux_session_name(task_id)
    td = cwd or str(task_dir(task_id))

    # Per-task hooks (Stop, Notification) → ~/.taskpilot/<id>/state/agent.json
    hook_settings = write_hook_settings(task_id)

    # Build plugin-dir flags
    plugin_flags = ""
    for p in plugins:
        plugin_flags += f" --plugin-dir {p}"

    # Build model flag
    model_flag = f" --model {model}" if model else ""

    # Build dev channels flag — session-bridge is the only channel.
    # Loaded as plugin:session-bridge@softwaresoftware-plugins (marketplace
    # form) so it auto-resolves via /softwaresoftware:install instead of
    # requiring a hand-edited user MCP entry. The dangerously-load flag is
    # still needed to bypass the channel allowlist for inbound notifications.
    all_channels = ["plugin:session-bridge@softwaresoftware-plugins"]
    for ch in (channels or []):
        if ch not in all_channels:
            all_channels.append(ch)

    # Verify each channel reference resolves before launching. Skipping this
    # check produces a deaf agent (channel.mjs never loads → no bridge
    # registration → silent failure 20s later).
    validate_channels(all_channels)

    channels_arg = " ".join(all_channels)

    labels = _session_labels(kind)

    # The tmux command: while-loop for crash recovery
    # rotation.py handles respawn decisions
    # Export TASKPILOT_TASK_ID so capability plugins can scope their storage
    # Export SESSION_NAME/SESSION_NAMESPACE/SESSION_LABELS so session-bridge
    # channel.mjs registers this agent under name=<task_id>, namespace=taskpilot
    # at /register time.
    cmd = f"""export TASKPILOT_TASK_ID={task_id}
export SESSION_NAME={task_id}
export SESSION_NAMESPACE={SESSION_NAMESPACE}
export SESSION_LABELS={labels}
while true; do
  cd {td} && \\
  claude --dangerously-skip-permissions \\
    --dangerously-load-development-channels {channels_arg} \\
    --settings {hook_settings} \\
    {plugin_flags}{model_flag} \\
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

    # Wait for session-bridge to register the session by name. If the channel
    # never comes up (validate_channels passed but something else went wrong —
    # bridge daemon down, claude crashed during boot, etc.), report failure
    # rather than silently returning success.
    if not wait_for_channel(task_id, timeout=20):
        return False

    # Brief settle time for MCP connection
    time.sleep(3)
    return True


def send_initial_prompt(task_id: str, description: str) -> bool:
    """POST the initial task prompt via session-bridge."""
    payload = json.dumps({"text": description, "from_session": "taskpilot-spawner"})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
             "-d", payload, f"{SESSION_BRIDGE_URL}/sessions/{task_id}/message"],
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


def channel_healthy(task_id: str) -> bool:
    """Check if session-bridge has a registered channel for this task."""
    try:
        result = subprocess.run(
            ["curl", "-sf", f"{SESSION_BRIDGE_URL}/sessions/{task_id}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return data.get("channel_port") is not None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return False


def wait_for_channel(task_id: str, timeout: int = 20) -> bool:
    """Poll until the task's channel registers with session-bridge, or timeout.

    Returns True if the channel was healthy within the timeout, False otherwise.
    Caller is responsible for surfacing failure to its own caller — this just
    reports the truth instead of optimistically returning True like the old
    inline polling did.
    """
    for _ in range(timeout):
        if channel_healthy(task_id):
            return True
        time.sleep(1)
    return False


class ChannelResolutionError(RuntimeError):
    """Raised when a --channels reference can't be resolved to a loadable MCP/plugin.

    A spawn that proceeds with an unresolvable channel produces a "deaf" agent:
    Claude warns "no MCP server configured with that name" and the channel.mjs
    that would register the session with the bridge never starts. The agent is
    alive in tmux but unreachable from the mesh. Validating up front turns that
    silent failure into an explicit error at spawn time.
    """


def validate_channels(channels: list[str]) -> None:
    """Raise ChannelResolutionError if any channel reference doesn't resolve.

    Channel forms:
      server:<name>          — must be a configured MCP in ~/.claude.json
      plugin:<name>@<market> — must be installed per installed_plugins.json

    Anything else passes (forward-compat with future channel kinds).
    """
    if not channels:
        return

    claude_json = _read_json(CLAUDE_JSON) or {}
    user_mcps = set((claude_json.get("mcpServers") or {}).keys())

    installed = _read_json(INSTALLED_PLUGINS_PATH) or {}
    installed_plugins = {key.split("@")[0] for key in installed.get("plugins", {}).keys()}

    for ch in channels:
        if ch.startswith("server:"):
            name = ch[len("server:"):]
            if name not in user_mcps:
                raise ChannelResolutionError(
                    f"channel '{ch}' references MCP server '{name}' which is not "
                    f"configured. Add it via `claude mcp add {name} ...` or remove "
                    f"this channel from the spawn config."
                )
        elif ch.startswith("plugin:"):
            spec = ch[len("plugin:"):]
            name = spec.split("@", 1)[0]
            if name not in installed_plugins:
                raise ChannelResolutionError(
                    f"channel '{ch}' references plugin '{name}' which is not "
                    f"installed. Install via `/softwaresoftware:install {name}` "
                    f"(or `claude plugin install {spec}`) or remove this channel."
                )


# ---------------------------------------------------------------------------
# systemd service lifecycle for kind=service
# ---------------------------------------------------------------------------

# Resolve claude binary path at import time (same pattern as NODE_BIN)
_claude = shutil.which("claude")
if not _claude:
    # Check common install locations
    for candidate in [Path.home() / ".local" / "bin" / "claude", Path("/usr/local/bin/claude")]:
        if candidate.exists():
            _claude = str(candidate)
            break
CLAUDE_BIN = _claude or "claude"


def _systemd_unit_name(task_id: str) -> str:
    return task_id


def _systemd_unit_path(task_id: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{_systemd_unit_name(task_id)}.service"


def write_service_script(
    task_id: str,
    plugins: list[str],
    model: str | None = None,
    cwd: str | None = None,
    channels: list[str] | None = None,
    kind: str = "service",
) -> Path:
    """Generate start.sh for a kind=service agent.

    Messaging routes through session-bridge (localhost:8910). Agent is
    addressable by task_id because we export SESSION_NAME=<task_id>; the
    session-bridge channel.mjs reads that env var at /register time.
    """
    td = task_dir(task_id)
    td.mkdir(parents=True, exist_ok=True)
    script_path = td / "start.sh"

    session = tmux_session_name(task_id)
    project_dir = cwd or str(td)

    # Per-task hooks (Stop, Notification) → ~/.taskpilot/<id>/state/agent.json
    hook_settings = write_hook_settings(task_id)

    # Build plugin-dir flags
    plugin_flags = ""
    for p in plugins:
        plugin_flags += f" --plugin-dir {p}"

    # Build model flag
    model_flag = f" --model {model}" if model else ""

    # Build dev channels — session-bridge loaded via marketplace plugin form.
    # See spawn_tmux() for the why behind plugin: vs server: choice.
    all_channels = ["plugin:session-bridge@softwaresoftware-plugins"]
    for ch in (channels or []):
        if ch not in all_channels:
            all_channels.append(ch)

    # Validate before writing the script — generating a start.sh that
    # systemd will faithfully respawn forever with broken channel refs is
    # worse than failing here.
    validate_channels(all_channels)

    channels_arg = " ".join(all_channels)

    labels = _session_labels(kind)

    script = f"""#!/usr/bin/env bash
# Auto-generated by taskpilot for service: {task_id}
# Do not edit — regenerated on each spawn.
set -euo pipefail

# Source user env for API keys, nvm, PATH
# bashrc exits early for non-interactive shells, so source nvm directly
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"
source "$HOME/.bashrc" 2>/dev/null || true

SESSION="{session}"
PROJECT_DIR="{project_dir}"
CLAUDE="{CLAUDE_BIN}"
TASK_ID="{task_id}"
SESSION_BRIDGE_URL="http://127.0.0.1:8910"

# Kill stale tmux session if any
tmux kill-session -t "$SESSION" 2>/dev/null || true
sleep 1

# Register project-scoped MCPs from cwd/.claude/settings.json into ~/.claude.json
# so claude picks them up at launch. We track which we added in project_mcps.json
# so kill_task can clean them up on teardown.
python3 -c "
import json, shutil
from pathlib import Path

p = Path.home() / '.claude.json'
d = json.loads(p.read_text())
d.setdefault('mcpServers', {{}})

# Resolve node/npx commands to absolute paths since claude's MCP spawner
# doesn't inherit the shell PATH (nvm etc won't be available)
_node_cmds = {{'node', 'npx', 'tsx'}}
def _resolve_cmd(cmd):
    if cmd in _node_cmds:
        resolved = shutil.which(cmd)
        if resolved:
            return resolved
    return cmd

project_settings = Path('{project_dir}') / '.claude' / 'settings.json'
_taskpilot_project_mcps = []
if project_settings.exists():
    try:
        ps = json.loads(project_settings.read_text())
        for name, cfg in ps.get('mcpServers', {{}}).items():
            cfg = dict(cfg)
            cfg['command'] = _resolve_cmd(cfg.get('command', ''))
            d['mcpServers'][name] = cfg
            _taskpilot_project_mcps.append(name)
    except Exception:
        pass
(Path.home() / '.taskpilot' / '{task_id}' / 'project_mcps.json').write_text(
    json.dumps(_taskpilot_project_mcps)
)

p.write_text(json.dumps(d, indent=2))
"

# Start Claude in tmux with crash-recovery while-loop
# SESSION_NAME/SESSION_NAMESPACE/SESSION_LABELS are read by session-bridge
# channel.mjs at /register time, naming this agent and putting it in the
# taskpilot namespace.
tmux new-session -d -s "$SESSION" -c "$PROJECT_DIR" \\
    "bash -lc 'export TASKPILOT_TASK_ID={task_id}; export SESSION_NAME={task_id}; export SESSION_NAMESPACE={SESSION_NAMESPACE}; export SESSION_LABELS={labels}; while true; do cd {project_dir} && \\
    {CLAUDE_BIN} --dangerously-skip-permissions \\
    --dangerously-load-development-channels {channels_arg} \\
    --settings {hook_settings} \\
    {plugin_flags}{model_flag} \\
    --name {task_id}; \\
    python {PLUGIN_ROOT / "rotation.py"} {task_id} || break; sleep 5; done'"

# Wait for Claude to initialize (trust dialog + MCP servers + plugins)
sleep 20

# Auto-accept trust dialog
tmux send-keys -t "$SESSION" Enter
sleep 4

# Auto-accept channels warning
tmux send-keys -t "$SESSION" Enter

# Wait for session-bridge to see the task by name with a live channel port.
# Naming comes from the SESSION_NAME env var exported above, which
# channel.mjs reads and includes in its /register payload.
for i in $(seq 1 30); do
    if curl -sf -m 2 "$SESSION_BRIDGE_URL/sessions/$TASK_ID" \\
         | python3 -c "import sys, json; d = json.load(sys.stdin); sys.exit(0 if d.get('channel_port') else 1)" \\
         2>/dev/null; then
        break
    fi
    sleep 1
done

# Brief settle time
sleep 3

# Send initial task prompt via session-bridge
if [ -f "$HOME/.taskpilot/$TASK_ID/prompt.txt" ]; then
    PROMPT_BODY=$(python3 -c "
import json, sys
text = open('$HOME/.taskpilot/$TASK_ID/prompt.txt').read()
sys.stdout.write(json.dumps({{'text': text, 'from_session': 'taskpilot-spawner'}}))
")
    curl -s -X POST -H 'Content-Type: application/json' \\
         -d "$PROMPT_BODY" \\
         "$SESSION_BRIDGE_URL/sessions/$TASK_ID/message" > /dev/null || true
fi

echo "taskpilot service '$TASK_ID' started in tmux session '$SESSION'"

# Keep service alive by tailing the tmux session (same pattern as beats-dj)
while tmux has-session -t "$SESSION" 2>/dev/null; do
    sleep 30
done

echo "taskpilot service '$TASK_ID' ended"
"""

    script_path.write_text(script)
    script_path.chmod(0o755)
    return script_path


def install_service(task_id: str) -> bool:
    """Generate and enable a systemd user service for a taskpilot service agent."""
    unit_name = _systemd_unit_name(task_id)
    unit_path = _systemd_unit_path(task_id)
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    start_script = task_dir(task_id) / "start.sh"
    session = tmux_session_name(task_id)

    unit_content = f"""[Unit]
Description=taskpilot service: {task_id}
After=network.target

[Service]
Type=simple
ExecStart={start_script}
ExecStop=/usr/bin/tmux kill-session -t {session}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit_content)

    # Reload, enable, start
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", unit_name], capture_output=True)
    subprocess.run(["systemctl", "--user", "start", unit_name], capture_output=True)

    # Ensure user services start at boot even before login
    user = os.environ.get("USER", "")
    if user:
        subprocess.run(["loginctl", "enable-linger", user], capture_output=True)

    return True


def uninstall_service(task_id: str) -> bool:
    """Stop, disable, and remove the systemd user service for a taskpilot service agent."""
    unit_name = _systemd_unit_name(task_id)
    unit_path = _systemd_unit_path(task_id)

    subprocess.run(["systemctl", "--user", "stop", unit_name], capture_output=True)
    subprocess.run(["systemctl", "--user", "disable", unit_name], capture_output=True)

    if unit_path.exists():
        unit_path.unlink()

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return True


def is_service_active(task_id: str) -> bool:
    """Check if the systemd user service is active."""
    unit_name = _systemd_unit_name(task_id)
    result = subprocess.run(
        ["systemctl", "--user", "is-active", unit_name],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "active"


# ---------------------------------------------------------------------------
# Cross-host spawn forwarding
# ---------------------------------------------------------------------------


def _list_mesh_hosts() -> list[dict]:
    """GET /hosts from the local session-bridge daemon. Returns [] on failure."""
    req = urllib.request.Request(f"{SESSION_BRIDGE_URL}/hosts", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def lookup_peer_url(host: str) -> str | None:
    """Resolve a mesh hostname to the URL of its session-bridge daemon.

    Self-host always returns the loopback URL — there is no benefit to
    routing through the tailnet IP for local calls. Unknown hosts and
    unreachable session-bridge return None so callers can fail clearly.
    """
    hosts = _list_mesh_hosts()
    for h in hosts:
        if h.get("host") == host:
            if h.get("self"):
                return SESSION_BRIDGE_URL
            ip = h.get("ip")
            port = h.get("port") or 8910
            if not ip:
                return None
            return f"http://{ip}:{port}"
    return None


def is_self_host(host: str) -> bool:
    """Return True if `host` is the local daemon's canonical hostname."""
    for h in _list_mesh_hosts():
        if h.get("host") == host:
            return bool(h.get("self"))
    return False


def spawn_remote(task: dict) -> dict:
    """Forward a spawn to the peer named in `task['host']` via /spawn.

    The peer's session-bridge daemon does the tmux + claude work and
    waits for registration; we just pass through the result. The agent
    becomes mesh-addressable as `<task_id>.taskpilot.<host>` once the
    peer reports back.

    Pre-PR-4-on-peer ports of session-bridge will return 404 here; the
    error surfaces to the caller as a clear `spawned: false`.
    """
    if task.get("kind") == "service":
        return {"spawned": False, "error": "kind=service not supported for remote spawn yet (no remote systemd install)"}

    host = task.get("host")
    if not host:
        return {"spawned": False, "error": "no host set on task"}

    url = lookup_peer_url(host)
    if not url:
        return {"spawned": False, "error": f"host '{host}' is not in the mesh (peers.json or session-bridge unreachable)"}

    payload = {
        "name": task["task_id"],
        "namespace": "taskpilot",
        "labels": ["kind:task"],
        "model": task.get("model"),
        "initial_message": task.get("description"),
    }
    body = json.dumps({k: v for k, v in payload.items() if v is not None}).encode()
    req = urllib.request.Request(
        f"{url}/spawn",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", str(e))
        except (json.JSONDecodeError, ValueError):
            detail = str(e)
        return {"spawned": False, "error": f"peer {host} returned {e.code}: {detail}"}
    except urllib.error.URLError as e:
        return {"spawned": False, "error": f"peer {host} unreachable: {e.reason}"}
