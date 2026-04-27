#!/usr/bin/env python3
"""Context rotation decision script.

Called after Claude exits in the tmux while-loop.
Exit 0 = respawn (continue). Exit 1 = stop (break).

Decision sources, in priority order:
  1. DB status — killed/paused tasks never respawn.
  2. state.json (`phase` written by the agent itself) — explicit completion.
  3. state/agent.json `last_stop.last_assistant_message` — implicit completion
     detected from the agent's final turn (Stop hook).
  4. Default — respawn.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store

# Phrases an agent uses when it's wrapping up. Conservative list — false
# positives here cause premature task completion. We require one of these
# to appear in the final ~400 chars of the last assistant message.
COMPLETION_PATTERNS = [
    r"\btask (?:is )?(?:complete|completed|done|resolved|finished)\b",
    r"\b(?:all|everything) done\b",
    r"\bnothing (?:left|else) to do\b",
    r"\bfinished (?:the )?(?:task|work|job)\b",
    r"\bwrapping up\b",
    r"\bmarking (?:this |the )?(?:task )?complete\b",
]
_COMPLETION_RE = re.compile("|".join(COMPLETION_PATTERNS), re.IGNORECASE)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _explicit_completion(task_id: str) -> bool:
    """state.json with phase=done|completed → the agent declared itself done."""
    state = _read_json(Path.home() / ".taskpilot" / task_id / "state.json")
    if not state:
        return False
    phase = (state.get("phase") or "").lower()
    return phase in ("done", "completed")


def _implicit_completion(task_id: str) -> bool:
    """Stop hook captured a completion-shaped final message."""
    agent = _read_json(Path.home() / ".taskpilot" / task_id / "state" / "agent.json")
    if not agent:
        return False
    last_stop = agent.get("last_stop") or {}
    msg = last_stop.get("last_assistant_message") or ""
    if not msg:
        return False
    # Match against the tail — agents tend to declare completion at the end.
    tail = msg[-400:]
    return bool(_COMPLETION_RE.search(tail))


def _mark_completed(task_id: str) -> None:
    conn = store.get_db()
    store.update_status(conn, task_id, "completed")
    conn.close()


def should_respawn(task_id: str) -> bool:
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()

    if not task:
        return False

    # Externally killed/paused — never respawn.
    if task["status"] not in ("running",):
        return False

    if _explicit_completion(task_id) or _implicit_completion(task_id):
        _mark_completed(task_id)
        return False

    # Increment invocation count and continue.
    conn = store.get_db()
    store.increment_invocation(conn, task_id)
    conn.close()
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <task_id>", file=sys.stderr)
        sys.exit(1)

    task_id = sys.argv[1]
    if should_respawn(task_id):
        sys.exit(0)  # continue loop
    else:
        sys.exit(1)  # break loop
