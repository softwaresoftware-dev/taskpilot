"""Side-effecting actions taken in response to classified agent state.

Called from hooks/on-stop.py. Each function is fire-and-forget where
possible — failures should never crash the hook.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spawner
import store

logger = logging.getLogger(__name__)

# Honor $TASKPILOT_HOME (set by the spawner) so hook scripts running inside
# a sandboxed agent write escalations.jsonl to the real ~/.taskpilot/<id>/,
# not to the nested ~/.taskpilot/<id>/.taskpilot/<id>/ that
# `Path.home() / .taskpilot` resolves to inside the sandbox.
TASKPILOT_DIR = Path(os.environ["TASKPILOT_HOME"]) if os.environ.get("TASKPILOT_HOME") else Path.home() / ".taskpilot"

# Time to wait after toggling pipe-pane off before writing the completion
# separator. tmux closes the pipe FD synchronously; the cat child receives
# EOF and exits within microseconds-to-milliseconds. 100 ms is conservative
# for kernel scheduler jitter under load.
_PIPE_DRAIN_SECONDS = 0.1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_separator(pane_log: Path, text: str) -> None:
    """Append a separator line to pane.log via os.open(O_CREAT, 0o600).

    Single os.open ensures the file is born at mode 0600 even if subsequent
    writes fail — there is no chmod-after-create race.
    """
    pane_log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(pane_log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _legacy_capture(pane_log: Path, session: str) -> None:
    """Pre-kill flush for tasks where pipe-pane was never attached.

    No concurrent writer exists (pipe-pane absent), so capture-pane is safe.
    Single os.open guarantees mode 0600 even if capture-pane raises mid-write.
    """
    pane_log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(pane_log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(
            fd,
            f"=== legacy capture (no live tee) at {_now_iso()} ===\n".encode(),
        )
        # subprocess.run with stdout=fd: capture-pane writes directly to the
        # same fd. With O_APPEND, every write atomically seeks to EOF, so
        # the header above and the capture below cannot interleave.
        subprocess.run(
            ["tmux", "capture-pane", "-t", f"{session}:0.0", "-p", "-S", "-"],
            stdout=fd,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        os.fsync(fd)
    finally:
        os.close(fd)


def mark_completed_and_kill(task_id: str) -> None:
    """Mark the task completed in the DB and tear down its tmux session.

    Flushes pane.log before kill via one of two paths:
      - steady: pipe-pane was attached this invocation (sentinel present).
        Toggle off pipe-pane, drain, append completion separator, then kill.
      - legacy: pipe-pane never attached (sentinel absent). Capture pane
        scrollback into pane.log, append completion separator, then kill.

    Detaches the tmux kill so it survives our own death — the hook is running
    inside the agent's process tree, and tearing tmux down here will SIGHUP
    the chain that includes us.
    """
    try:
        conn = store.get_db()
        store.update_status(conn, task_id, "completed")
        conn.close()
    except Exception:
        pass

    session = spawner.tmux_session_name(task_id)
    pane_log = spawner.pane_log_path(task_id)
    sentinel = spawner.pane_log_sentinel(task_id)
    pipe_attached = sentinel.exists()

    if pipe_attached:
        # Steady path: pipe-pane has been streaming. Toggle off so cat gets
        # EOF, drains its kernel-pipe buffer, and exits. Brief sleep is
        # conservative for OS scheduler jitter; kernel pipe drains are
        # typically sub-millisecond.
        try:
            subprocess.run(
                ["tmux", "pipe-pane", "-t", f"{session}:0.0"],
                check=False,
                capture_output=True,
                timeout=2,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("pipe-pane toggle-off failed for %s: %s", task_id, e)
        time.sleep(_PIPE_DRAIN_SECONDS)
        try:
            sentinel.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("sentinel unlink failed for %s: %s", task_id, e)
    else:
        # Legacy / failed-attach: capture-pane is the only way to preserve
        # scrollback. Wrapped in try/except so any failure here cannot block
        # the kill below.
        try:
            _legacy_capture(pane_log, session)
        except Exception as e:  # noqa: BLE001
            logger.warning("legacy capture failed for %s: %s", task_id, e)

    # Append completion separator (both paths). Wrapped so failure cannot
    # block the kill.
    try:
        _append_separator(
            pane_log,
            f"\n=== completed at {_now_iso()} ===\n",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("completion separator write failed for %s: %s", task_id, e)

    # Detached kill — same as before.
    try:
        subprocess.Popen(
            ["tmux", "kill-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def notify_human(task_id: str, message: str) -> None:
    """Record an escalation and (optionally) shell out to a notification command.

    Always appends to ~/.taskpilot/<task_id>/escalations.jsonl — durable,
    pollable by taskboard / dashboards / human eyeballs.

    If TASKPILOT_NOTIFY_CMD is set in the agent's env, we run it detached
    with TASKPILOT_TASK_ID and TASKPILOT_MESSAGE exported. Lets the user
    plug in any notification transport (Slack webhook, phone bridge,
    notify-send, etc.) without taskpilot needing to know which.
    """
    record = {
        "task_id": task_id,
        "at": _now_iso(),
        "message": message,
    }

    log_path = TASKPILOT_DIR / task_id / "escalations.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

    cmd = os.environ.get("TASKPILOT_NOTIFY_CMD")
    if not cmd:
        return

    try:
        env = {**os.environ, "TASKPILOT_TASK_ID": task_id, "TASKPILOT_MESSAGE": message}
        subprocess.Popen(
            ["sh", "-c", cmd],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
