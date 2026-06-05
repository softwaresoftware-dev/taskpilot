"""L3 end-to-end integration test — spawns a REAL claude agent.

This is the only test layer that exercises the actual runtime: a real `claude`
process in a real tmux session, real Claude Code hook dispatch, and real
session-bridge channel delivery. It runs the real thing and asserts on real
on-disk artifacts.

What it catches (deterministically, in the real runtime):
  * wiring breakage — hooks not firing, wrong event shape, channel not
    delivering.

Gating — opt-in; uses tokens, network, OAuth, and ~30-90s wall clock:
  - TASKPILOT_E2E=1 must be set
  - `claude` must be on PATH
  - the taskpilot daemon and session-bridge must be reachable
Otherwise the whole module skips.

Run:  TASKPILOT_E2E=1 make test-e2e
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
import spawner  # noqa: E402
import store  # noqa: E402

DAEMON_URL = os.environ.get("TASKPILOT_DAEMON_URL", "http://127.0.0.1:8912")
BRIDGE_URL = spawner.SESSION_BRIDGE_URL


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _post(url: str, body: dict, timeout: float = 20.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()


def _wait(pred, timeout: float, interval: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# --- module-level skip gating -------------------------------------------------
_skips: list[str] = []
if os.environ.get("TASKPILOT_E2E", "").lower() not in ("1", "true", "yes"):
    _skips.append("set TASKPILOT_E2E=1 to run (real claude: tokens, auth, ~60s)")
if shutil.which("claude") is None:
    _skips.append("`claude` not on PATH")
if not _http_ok(f"{DAEMON_URL}/health"):
    _skips.append(f"taskpilot daemon unreachable at {DAEMON_URL}")
if not _http_ok(f"{BRIDGE_URL}/health"):
    _skips.append(f"session-bridge unreachable at {BRIDGE_URL}")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(bool(_skips), reason="; ".join(_skips)),
]


@pytest.fixture
def real_agent():
    """Create + spawn a real one-shot agent with a brief; always tear it down.

    The description drives a fast, tool-free, deterministic reply so the Stop
    hook fires quickly.
    """
    tid = "e2e-" + uuid.uuid4().hex[:8]
    brief = {
        "objectives": ["Reply with the single word: done"],
        "success_criteria": ["The agent has replied with the word 'done'"],
        "boundaries": ["Do not create files", "Do not run tools", "Do not ask questions"],
    }
    description = (
        "Automated integration probe. Reply with exactly the single word: done . "
        "Do not run any tools, do not create files, do not ask questions."
    )
    rec = server.create_task(
        name=tid, description=description, operating_brief=brief, kind="task"
    )
    assert isinstance(rec, dict) and rec.get("task_id"), f"create_task failed: {rec}"
    tid = rec["task_id"]

    yield tid, brief

    # Teardown — best-effort, always runs. Scope every kill to OUR exact session
    # name (never a broad pattern) so we can't touch another task.
    try:
        _post(f"{DAEMON_URL}/tasks/{tid}/kill", {}, timeout=15)
    except Exception:
        pass
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", spawner.tmux_session_name(tid)],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass
    shutil.rmtree(spawner.task_dir(tid), ignore_errors=True)
    try:
        conn = store.get_db()
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (tid,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def test_real_agent_runtime_artifacts(real_agent):
    tid, _brief = real_agent
    real_taskpilot = Path(os.path.expanduser("~")) / ".taskpilot"
    task_dir = real_taskpilot / tid
    agent_json = task_dir / "state" / "agent.json"

    # Spawn through the real daemon path — this launches claude in tmux and
    # POSTs the description as the initial prompt over the session-bridge channel.
    status, body = _post(f"{DAEMON_URL}/tasks/{tid}/spawn", {}, timeout=30)
    assert status == 200, f"spawn failed: {status} {body}"

    # 1) Hooks fire AND write to ~/.taskpilot/<id>/state/agent.json.
    assert _wait(lambda: agent_json.exists(), timeout=120), (
        f"agent.json never appeared at {agent_json} — hooks didn't fire"
    )

    # 2) Stop hook captured the assistant's final message — proves Claude Code
    #    fires the hook with the event shape on-stop.py expects.
    def stop_recorded() -> bool:
        try:
            d = json.loads(agent_json.read_text())
            return bool(d.get("last_stop", {}).get("last_assistant_message"))
        except Exception:
            return False

    assert _wait(stop_recorded, timeout=60), (
        "Stop hook never recorded last_assistant_message in agent.json"
    )
