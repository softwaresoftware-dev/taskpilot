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
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

TASKPILOT_DIR = Path(os.environ.get("TASKPILOT_DATA_DIR", str(Path.home() / ".taskpilot")))
# When set, agents are spawned with HOME overridden to this path.
# The path should contain a minimal ~/.claude with workspace-specific settings.json
# and a symlink to ~/.claude/plugins so installed plugins remain accessible.
TASKPILOT_AGENT_HOME = os.environ.get("TASKPILOT_AGENT_HOME", "")
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


# Sandboxed tasks keep their launch files (MCP config, settings, the HTTP
# tool's credentials) here — outside every task dir and every agent cwd, so a
# sandboxed agent's file tools (confined to its cwd) can never read them.
SANDBOX_DIR = TASKPILOT_DIR / ".sandbox"
SANDBOX_TOOLS = {"Read", "Glob", "Grep", "Write", "Edit", "TodoWrite"}   # nothing that runs code or reaches the web
SANDBOX_DEFAULT_TOOLS = ["Read", "Glob", "Grep"]
CHANNEL_PLUGIN = "session-bridge@softwaresoftware-plugins"
CHANNEL_TOOLS = "mcp__plugin_session-bridge_session-bridge__"


def sandbox_dir(task_id: str) -> Path:
    return SANDBOX_DIR / task_id


def _private_write(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def sandbox_launch(task_id: str, sandbox: dict) -> tuple[str, str]:
    """Write a sandboxed task's private launch files and return (env exports,
    claude flags).

    The agent gets: only the built-in tools in `sandbox["tools"]` (file tools,
    confined by --restricted to its cwd), its session-bridge channel with
    `reply` only (no `message`/`sessions` — it cannot reach other sessions),
    and, when `sandbox["http"]` is set, one `http` tool limited to the listed
    method + path prefixes on one base URL (sandbox_http.py adds the headers,
    so the agent never sees the credential). No Bash, no web, none of the
    user's MCP servers or claude.ai connectors, no user/project settings or
    hooks, and anything else is denied without a prompt (dontAsk)."""
    tools = sandbox.get("tools") or SANDBOX_DEFAULT_TOOLS
    bad = sorted(set(tools) - SANDBOX_TOOLS)
    if bad:
        raise ValueError(f"sandbox tools not allowed: {', '.join(bad)}")
    d = sandbox_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(SANDBOX_DIR, 0o700)
    os.chmod(d, 0o700)
    servers: dict = {}
    allowed = [f"{CHANNEL_TOOLS}reply"]
    if sandbox.get("http"):
        _private_write(d / "http.json", sandbox["http"])
        servers["sandbox"] = {
            "command": sys.executable,
            "args": [str(Path(__file__).resolve().parent / "sandbox_http.py")],
            "env": {"TASKPILOT_SANDBOX_HTTP": str(d / "http.json")},
        }
        allowed.append("mcp__sandbox__http")
    _private_write(d / "mcp.json", {"mcpServers": servers})
    # --restricted ignores user settings, which is where the channel plugin is
    # enabled; enable exactly that one plugin here.
    _private_write(d / "settings.json", {"enabledPlugins": {CHANNEL_PLUGIN: True}})
    env = "export ENABLE_CLAUDEAI_MCP_SERVERS=false\n"
    flags = (f"--restricted --tools {','.join(tools)} --permission-mode dontAsk "
             f"--mcp-config {shlex.quote(str(d / 'mcp.json'))} --settings {shlex.quote(str(d / 'settings.json'))} "
             f"--allowedTools {' '.join(allowed)} "
             f"--disallowedTools {CHANNEL_TOOLS}message {CHANNEL_TOOLS}sessions")
    return env, flags


def redact_sandbox(raw: str | dict | None) -> dict | None:
    """A sandbox spec safe to return from the API: header values masked."""
    if not raw:
        return None
    sb = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if sb.get("http"):
        sb["http"] = {**sb["http"], "headers": {k: "…" for k in (sb["http"].get("headers") or {})}}
    return sb


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



# Managed-settings locations (Claude Code reads these machine-wide policies).
_MANAGED_SETTINGS = {
    "darwin": "/Library/Application Support/ClaudeCode/managed-settings.json",
    "win32": r"C:\Program Files\ClaudeCode\managed-settings.json",
}.get(sys.platform, "/etc/claude-code/managed-settings.json")


def channel_flag(plugin: str = "session-bridge",
                 marketplace: str = "softwaresoftware-plugins",
                 managed_path: str = _MANAGED_SETTINGS) -> str:
    """The flag that loads the session-bridge channel.

    `--channels` (no startup warning) only works for allowlisted channel
    plugins. Use it when a managed policy enables channels and allowlists
    session-bridge; otherwise fall back to the development-channels flag,
    whose warning dialog wait_for_ready() dismisses.
    """
    try:
        with open(managed_path) as f:
            policy = json.load(f)
    except (OSError, ValueError):
        policy = {}
    allowed = policy.get("allowedChannelPlugins") or []
    if policy.get("channelsEnabled") and any(
        a.get("plugin") == plugin and a.get("marketplace") == marketplace
        for a in allowed if isinstance(a, dict)
    ):
        return "--channels"
    return "--dangerously-load-development-channels"


# Startup dialogs an unattended session can hit: (detector, accept-option label).
# The highlighted default is NOT reliably the accept option — the trust dialog
# defaults to "No, exit" — so the option is chosen by its label.
_STARTUP_DIALOGS = {
    "trust": (re.compile(r"Quick safety check", re.I), "Yes, I trust this folder"),
    "dev-channels": (re.compile(r"Loading development channels", re.I),
                     "I am using this for local development"),
}
_CURSOR = "❯"
_PROMPT_READY = re.compile(r"^\s*❯", re.M)


def _select_option(session: str, pane: str, label: str) -> None:
    """Move the dialog cursor onto the option labelled `label`, then Enter."""
    lines = pane.splitlines()
    cur = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(_CURSOR)), None)
    want = next((i for i, ln in enumerate(lines) if label in ln), None)
    keys = []
    if cur is not None and want is not None and want != cur:
        keys = ["Down" if want > cur else "Up"] * abs(want - cur)
    subprocess.run(["tmux", "send-keys", "-t", session, *keys, "Enter"])


def wait_for_ready(session: str, timeout: float = 45.0) -> bool:
    """Dismiss whatever startup dialogs actually appear, until the prompt is up.

    Replaces fixed sleeps + blind Enters: with pre-trusted dirs and an
    allowlisted channel no dialog appears and this returns in a few seconds.
    """
    deadline = time.monotonic() + timeout
    answered: set[str] = set()
    while time.monotonic() < deadline:
        pane = subprocess.run(["tmux", "capture-pane", "-p", "-t", session],
                              capture_output=True, text=True).stdout
        dialog = next((k for k, (rx, _) in _STARTUP_DIALOGS.items() if rx.search(pane)), None)
        if dialog:
            if dialog not in answered:
                log.info("%s: accepting %s dialog", session, dialog)
                answered.add(dialog)
            _select_option(session, pane, _STARTUP_DIALOGS[dialog][1])
            time.sleep(1.5)
            continue
        if _PROMPT_READY.search(pane):
            return True
        time.sleep(0.5)
    log.warning("%s: prompt not ready after %.0fs", session, timeout)
    return False


def spawn_tmux(task_id: str, plugins: list[str], model: str | None = None,
               cwd: str | None = None, home: str | None = None,
               sandbox: dict | None = None) -> bool:
    """Launch the Claude session in tmux. Messaging goes through session-bridge.

    Unsandboxed, the agent inherits the user's real ~/.claude environment
    (global CLAUDE.md, rules, installed plugins, registered MCP servers) and
    skips every permission check. With `sandbox`, see sandbox_launch: file
    tools in its cwd, its channel, and at most one allowlisted HTTP tool.
    """
    session = tmux_session_name(task_id)
    # Default cwd is the task dir; an explicit cwd points the agent at a real
    # project instead.
    td = cwd or str(task_dir(task_id))

    # Build plugin-dir flags
    plugin_flags = ""
    for p in plugins if not sandbox else []:   # a sandbox loads no extra plugins
        plugin_flags += f" --plugin-dir {shlex.quote(p)}"
    sandbox_env, perm_flags = sandbox_launch(task_id, sandbox) if sandbox else ("", "--dangerously-skip-permissions")

    model_flag = f" --model {shlex.quote(model)}" if model else ""

    # session-bridge is the agent's channel — the messaging backbone. Loaded as
    # plugin:session-bridge@softwaresoftware-plugins (marketplace form); the
    # dangerously-load flag bypasses the channel allowlist for inbound
    # notifications. It's a hard dependency, so it's always installed.
    channels_arg = "plugin:session-bridge@softwaresoftware-plugins"
    channels_flag = channel_flag()

    # Env exports:
    #   TASKPILOT_TASK_ID — for capability plugins that scope storage per task
    #   SESSION_NAME      — read by session-bridge channel.mjs at /register
    #   SESSION_NAMESPACE — same
    #   CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false — no human is at the keyboard
    #     in a spawned agent, so the forked-suggestion LLM call is pure waste.
    # Per-task HOME (a workspace partition) takes precedence over the daemon-wide
    # TASKPILOT_AGENT_HOME fallback. This is what lets ONE taskpilot serve every
    # workspace: the agent's $HOME is its workspace partition, so it loads that
    # workspace's MCPs (.claude.json), connector skills (.claude/skills), and
    # vault (~/.mindframe/vault) — and nothing from any other workspace.
    eff_home = home or TASKPILOT_AGENT_HOME
    home_override = f"export HOME={shlex.quote(eff_home)}\n" if eff_home else ""
    # Spawned agents run on the Claude Code subscription. A stray (often invalid)
    # ANTHROPIC_API_KEY in the inherited env takes precedence over the
    # subscription login and silently breaks the agent, so clear it per-spawn —
    # robust no matter how the daemon's environment was created. Opt out with
    # TASKPILOT_KEEP_API_KEY=1 if an agent should use key-based auth instead.
    scrub = "" if os.environ.get("TASKPILOT_KEEP_API_KEY") else \
        "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN\n"
    cmd = f"""{home_override}{scrub}export TASKPILOT_TASK_ID={task_id}
export SESSION_NAME={task_id}
export SESSION_NAMESPACE={SESSION_NAMESPACE}
export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false
{sandbox_env}cd {shlex.quote(td)} && exec claude {perm_flags} \\
  {channels_flag} {channels_arg} \\
  {plugin_flags}{model_flag} \\
  --name {shlex.quote(task_id)}"""

    # cwd/home/model come from the API and are quoted above; the whole script
    # is quoted once more for the `sh -c` tmux wraps its command in.
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, f"bash -lc {shlex.quote(cmd)}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    wait_for_ready(session)
    if not is_tmux_alive(task_id):
        # `cd` failed or claude exited at once: the pane is gone, so this is a
        # failed spawn, not a running agent that never answers.
        log.warning("spawn %s: session exited during startup (bad cwd?)", task_id)
        return False

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
