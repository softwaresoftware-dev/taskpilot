"""L3 end-to-end integration test — spawns a REAL claude agent.

This is the only test layer that exercises the actual sandbox *runtime*: a real
`claude` process in a real tmux session under a redirected HOME, real Claude
Code hook dispatch, real session-bridge channel delivery, and the real
classifier judge. Unit tests run in an environment where `Path.home()` is the
developer's real home, so they are structurally blind to sandbox-context bugs.
This test runs the real thing and asserts on real on-disk artifacts.

What it catches (deterministically, in the real runtime):
  * the WRITE-leak class — any sandbox-executed module that resolves a path via
    `Path.home()` instead of `$TASKPILOT_HOME` and then writes/mkdirs will
    create a nested `~/.taskpilot/<id>/.taskpilot/` dir; we assert that never
    appears anywhere under the sandbox HOME. This is module-agnostic — it does
    not enumerate modules, so a NEW leaking module is caught automatically.
  * wiring breakage — hooks not firing, wrong event shape, channel not
    delivering, completion not tearing down tmux.

Known blind spot (be honest): a *read*-only leak (e.g. the classifier loading
brief.json from the wrong path) writes nothing, so the filesystem invariant
can't see it, and a real model resolves a "done" message as complete with or
without the brief. Read-leaks are pinned by the static AST guard (L1) and the
deterministic judge-prompt-capture test (L2), not here. See
docs / test_taskpilot_home_env.py.

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


def _get_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


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

    The brief carries success_criteria the classifier must load to judge the
    finish; the description drives a fast, tool-free, deterministic reply.
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


def test_real_agent_runtime_artifacts_and_completion(real_agent):
    tid, _brief = real_agent
    real_taskpilot = Path(os.path.expanduser("~")) / ".taskpilot"
    task_dir = real_taskpilot / tid
    agent_json = task_dir / "state" / "agent.json"

    # Spawn through the real daemon path — this launches claude in tmux and
    # POSTs the description as the initial prompt over the session-bridge channel.
    status, body = _post(f"{DAEMON_URL}/tasks/{tid}/spawn", {}, timeout=30)
    assert status == 200, f"spawn failed: {status} {body}"

    # 1) Hooks fire AND write to the REAL path (not the sandbox-nested path).
    #    Catches a _record/_state-dir Path.home() leak directly.
    assert _wait(lambda: agent_json.exists(), timeout=120), (
        f"agent.json never appeared at the real path {agent_json} — hooks either "
        f"didn't fire or wrote to a sandbox-nested path"
    )

    # 2) WRITE-leak invariant (module-agnostic): no nested ~/.taskpilot/<id>/.taskpilot/
    #    anywhere under the sandbox HOME. Any sandbox-executed module that wrote
    #    via Path.home() would create this. This is the durable class-catcher.
    nested = task_dir / ".taskpilot"
    assert not nested.exists(), (
        f"sandbox write-leak detected: a hook wrote to a Path.home()-derived "
        f"nested dir {nested} instead of $TASKPILOT_HOME"
    )

    # 3) Stop hook captured the assistant's final message — proves Claude Code
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

    # 4) Full classify→act pipeline: the agent finishing → classifier → resolved
    #    → mark_completed_and_kill flips DB status and tears down tmux.
    def completed() -> bool:
        rec = _get_json(f"{DAEMON_URL}/tasks/{tid}")
        return bool(rec and rec.get("status") == "completed")

    assert _wait(completed, timeout=120), (
        "task never reached 'completed' — classify→act completion pipeline broke"
    )

    # 5) tmux session is actually gone after completion.
    alive = (
        subprocess.run(
            ["tmux", "has-session", "-t", spawner.tmux_session_name(tid)],
            capture_output=True,
        ).returncode
        == 0
    )
    assert not alive, "tmux session still alive after task marked completed"
