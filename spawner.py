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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

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


# --- pane.log persistent log capture (v0.8.0) ---------------------------------
# Tmux pane buffers are ephemeral: they die with the session. We tee pane output
# to ~/.taskpilot/<task_id>/pane.log via `tmux pipe-pane` so `get_task_log` and
# downstream consumers (taskboard) can read agent history after the task is gone.

PANE_LOG_NAME = "pane.log"
PANE_LOG_MAX_BYTES_DEFAULT = 10 * 1024 * 1024
PANE_LOG_MIN_BYTES = 4096


def pane_log_path(task_id: str) -> Path:
    return task_dir(task_id) / PANE_LOG_NAME


def _pane_log_max_bytes() -> int:
    raw = os.environ.get("TASKPILOT_PANE_LOG_MAX_BYTES", "")
    try:
        return max(int(raw), PANE_LOG_MIN_BYTES) if raw else PANE_LOG_MAX_BYTES_DEFAULT
    except ValueError:
        return PANE_LOG_MAX_BYTES_DEFAULT


def _truncate_if_oversize(path: Path) -> None:
    """Soft cap on pane.log, enforced at spawn boundaries.

    Keeps the last cap/2 bytes plus a truncation marker. Atomic via tmp+rename
    (POSIX). Note: this does NOT fsync the parent directory; rename durability
    rests on the filesystem journal. Acceptable for a soft-cap log.

    Long-running services that don't crash will grow pane.log unboundedly
    between respawns — reconciler-side rotation is a v0.8.1 follow-up.
    """
    if not path.exists():
        return
    cap = _pane_log_max_bytes()
    size = path.stat().st_size
    if size <= cap:
        return
    keep = cap // 2
    with path.open("rb") as f:
        f.seek(size - keep)
        tail_bytes = f.read()
    now = datetime.now(timezone.utc).isoformat()
    marker = f"\n=== truncated {size - keep} bytes (cap={cap}) at {now} ===\n".encode()
    tmp = path.with_suffix(".log.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, marker)
        os.write(fd, tail_bytes)
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(path)


def _write_invocation_separator(path: Path, task_id: str) -> None:
    """Append `=== taskpilot invocation N at <iso> reason=start|respawn ===` to pane.log.

    Reads invocation_count from the DB. spawn_tmux runs BEFORE
    store.increment_invocation at every call site (daemon.py reconciler,
    daemon.py spawn endpoint, server.py direct fallback), so the DB count
    here is the *previous* invocation; the new invocation is db_count + 1.
    """
    # Lazy import to avoid circular deps at module load (store imports
    # nothing from spawner, but we keep the boundary clean).
    import store  # noqa: WPS433

    conn = store.get_db()
    try:
        task = store.get_task(conn, task_id)
        prev_count = (task or {}).get("invocation_count", 0)
    finally:
        conn.close()
    invocation = prev_count + 1
    reason = "start" if invocation == 1 else "respawn"
    now = datetime.now(timezone.utc).isoformat()
    sep = f"\n=== taskpilot invocation {invocation} at {now} reason={reason} ===\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, sep.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _install_pipe_pane(session: str, path: Path) -> bool:
    """Attach `tmux pipe-pane` from the session's first pane to `path`.

    Returns True if `tmux pipe-pane` exited 0; False otherwise. Failure is
    non-fatal: spawn must not depend on log infrastructure.

    `stdbuf -o0` is a *latency* optimization — without it, `cat` block-buffers
    writes to the file, delaying live `tail -f`. `cat` still flushes on EOF
    (when tmux closes the pipe FD), so no data is lost in either case.
    """
    quoted = shlex.quote(str(path))
    if shutil.which("stdbuf"):
        shell_cmd = f"stdbuf -o0 cat >> {quoted}"
    else:
        shell_cmd = f"cat >> {quoted}"
    target = f"{session}:0.0"
    try:
        result = subprocess.run(
            ["tmux", "pipe-pane", "-t", target, shell_cmd],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(
            f"taskpilot: pipe-pane install failed for {session}: {e}\n",
        )
        return False


def _setup_pane_log_capture(task_id: str, session: str) -> None:
    """Wire the pipe-pane tee for this invocation.

    Sequence: truncate (if oversized) → write invocation separator → attach
    pipe-pane. All steps are best-effort: spawn must not depend on log
    infrastructure.
    """
    path = pane_log_path(task_id)
    try:
        _truncate_if_oversize(path)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"taskpilot: pane.log truncate failed for {task_id}: {e}\n")
    try:
        _write_invocation_separator(path, task_id)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"taskpilot: separator write failed for {task_id}: {e}\n")
    _install_pipe_pane(session, path)


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
3. The human's reply arrives as a channel message — resume the blocked task when it arrives""")

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

    # Capability sections — describe intent only. Tool names live in the
    # MCP servers' own descriptions, which Claude Code auto-loads into the
    # agent's context. Listing them here would duplicate (and silently
    # drift from) the source of truth in each capability provider.
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

    if "scheduling" in capabilities:
        sections.append("""## Scheduling
Scheduling is available for recurring workflows on a cadence — daily research,
periodic checks, content schedules. Scheduled events arrive as channel
messages; process them when they arrive. Use an available skill or tool.""")

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


def _import_softwaresoftware():
    """Locate the installed softwaresoftware plugin and import its
    resolver + registry modules.

    Returns (resolver_module, registry_module).
    Raises RuntimeError if softwaresoftware (>= 1.4.0) isn't installed or
    can't be imported. taskpilot has a hard runtime dependency on it for
    capability resolution.

    Side effect: appends softwaresoftware's install path to sys.path
    (idempotent — only once per process).
    """
    installed = _read_json(INSTALLED_PLUGINS_PATH)
    entries = (installed or {}).get("plugins", {}).get("softwaresoftware@softwaresoftware-plugins") or []
    sw_path = (entries[0].get("installPath") if entries else "") or ""
    if not sw_path or not Path(sw_path).exists():
        raise RuntimeError(
            "softwaresoftware (>= 1.4.0) is required for capability resolution "
            "but isn't installed. Run: /softwaresoftware:install softwaresoftware"
        )

    import sys
    if sw_path not in sys.path:
        sys.path.append(sw_path)
    try:
        import resolver as sw_resolver
        import registry as sw_registry
    except ImportError as e:
        raise RuntimeError(f"softwaresoftware import failed: {e}")
    return sw_resolver, sw_registry


def resolve_capabilities(capabilities: list[str]) -> list[str]:
    """Resolve capability names to installed plugin directory paths.

    Delegates to softwaresoftware's `resolver.find_satisfier`. For each
    capability:

      * type=plugin → returns the plugin's installed path (added to --plugin-dir).
      * type=mcp    → satisfied by an already-loaded MCP server; nothing to add.
      * type=host   → cross-host satisfaction; out of scope for local spawn.
      * type=none   → no satisfier; silently skipped.

    Hard-fails (RuntimeError) if softwaresoftware isn't installed.
    """
    if not capabilities:
        return []

    sw_resolver, sw_registry = _import_softwaresoftware()
    resolved: list[str] = []
    seen: set[str] = set()
    for cap in capabilities:
        sat = sw_resolver.find_satisfier(cap)
        if sat.get("type") != "plugin":
            continue
        path = sw_registry.get_plugin_install_path(sat["name"])
        if not path:
            continue
        p = str(path)
        if p not in seen:
            resolved.append(p)
            seen.add(p)
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
               kind: str = "task",
               resume_session_id: str | None = None) -> bool:
    """Launch the Claude session in tmux. Messaging goes through session-bridge.

    The agent inherits the user's real ~/.claude environment (global CLAUDE.md,
    rules, installed plugins, registered MCP servers).

    resume_session_id: when set, launch with `claude --resume <id>` so the agent
    wakes back into its existing conversation (used to wake a dormant agent)."""
    session = tmux_session_name(task_id)
    # Default cwd is the task dir; an explicit cwd points the agent at a real
    # project instead.
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

    # When claude exits (cleanly or crashed), tmux exits with it. The
    # taskpilot daemon's reconciler is what decides whether to respawn:
    # kind=service tasks come back automatically on the next tick;
    # kind=task tasks get marked crashed. The Stop hook handles clean
    # completion (state.json phase=done) by flipping status=completed
    # before tmux ends, so the reconciler ignores them.
    #
    # Env exports:
    #   TASKPILOT_TASK_ID — for capability plugins that scope storage per task
    #   SESSION_NAME      — read by session-bridge channel.mjs at /register
    #   SESSION_NAMESPACE — same
    #   SESSION_LABELS    — same
    #   CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false — no human is at the keyboard
    #     in a spawned agent, so the forked-suggestion LLM call is pure waste.
    # Wake path: resume the agent's existing conversation instead of cold-starting.
    resume_flag = f" --resume {resume_session_id}" if resume_session_id else ""
    cmd = f"""export TASKPILOT_TASK_ID={task_id}
export SESSION_NAME={task_id}
export SESSION_NAMESPACE={SESSION_NAMESPACE}
export SESSION_LABELS={labels}
export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false
cd {td} && claude --dangerously-skip-permissions{resume_flag} \\
  --dangerously-load-development-channels {channels_arg} \\
  --settings {hook_settings} \\
  {plugin_flags}{model_flag} \\
  --name {task_id}"""

    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, f"bash -lc '{cmd}'"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    # Tee pane output to ~/.taskpilot/<task_id>/pane.log so post-completion
    # `get_task_log` can read history. Failure is non-fatal — spawn proceeds
    # even if the tee can't be installed.
    _setup_pane_log_capture(task_id, session)

    # Auto-accept trust dialog (option 1, "Yes, I trust this folder", is default).
    # The bypass-permissions warning is a one-time per-user prompt the user has
    # already accepted on their real ~/.claude, so the next thing claude shows is
    # the channels warning.
    time.sleep(7)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])

    # Auto-accept channels warning (default option is fine here)
    time.sleep(4)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])

    # Best-effort wait for session-bridge to register the channel by name.
    # Channel registration is eventually-consistent: channel.mjs re-registers
    # on its heartbeat whenever the bridge becomes reachable. So a slow or
    # momentarily-unreachable bridge is NOT a spawn failure — the tmux+claude
    # process is up, and the channel converges on its own once the bridge is
    # back. Treating the timeout as failure here (and letting the reconciler
    # mark the service 'crashed') was the cause of spurious "channel never
    # registered" crashes when a service was respawned while session-bridge
    # was briefly down. A successful tmux launch == spawned.
    if wait_for_channel(task_id, timeout=20):
        # Channel live — brief settle time for the MCP connection.
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


def capture_session_id(task_id: str, cwd: str | None = None) -> str | None:
    """The Claude session UUID for this agent, read from its newest transcript
    file. Returns None until the agent has run at least one turn. Used to resume
    on wake.

    Claude Code stores transcripts under ~/.claude/projects/<encoded-cwd>/, where
    the project dir name is the agent's cwd with every non-alphanumeric character
    replaced by '-' (e.g. /home/u/.taskpilot/x -> -home-u--taskpilot-x). The cwd
    defaults to the task dir; an explicit cwd (a real project) is encoded the
    same way."""
    launch_cwd = cwd or str(task_dir(task_id))
    encoded = re.sub(r"[^A-Za-z0-9]", "-", launch_cwd)
    proj = Path.home() / ".claude" / "projects" / encoded
    if not proj.exists():
        return None
    files = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


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
