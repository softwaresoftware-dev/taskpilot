"""Shared helpers for taskpilot hook scripts.

Hook scripts run in the agent's process tree, receive the hook event as JSON
on stdin, and must exit fast. We persist a structured snapshot to
~/.taskpilot/<task_id>/state/agent.json (latest event of each kind) plus
an append-only events.jsonl for audit/diagnostics.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def task_id() -> str | None:
    """Return the task id this agent runs under, or None if not in a taskpilot session."""
    return os.environ.get("TASKPILOT_TASK_ID") or None


def state_dir(tid: str) -> Path:
    d = Path.home() / ".taskpilot" / tid / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_event() -> dict | None:
    """Read the hook event JSON from stdin. Returns None on parse failure."""
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_record(tid: str, key: str, record: dict) -> None:
    """Update state/agent.json[key] = record and append to events.jsonl.

    agent.json is a small JSON document — read/modify/write is fine here.
    Concurrent hook firings on the same task are not expected (Claude fires
    Stop and Notification serially within a single session).
    """
    sd = state_dir(tid)
    agent_path = sd / "agent.json"

    try:
        current = json.loads(agent_path.read_text()) if agent_path.exists() else {}
    except json.JSONDecodeError:
        current = {}

    current[key] = record
    agent_path.write_text(json.dumps(current, indent=2))

    with (sd / "events.jsonl").open("a") as f:
        f.write(json.dumps({"key": key, **record}) + "\n")
