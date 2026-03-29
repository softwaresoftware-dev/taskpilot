#!/usr/bin/env python3
"""Context rotation decision script.

Called after Claude exits in the tmux while-loop.
Exit 0 = respawn (continue). Exit 1 = stop (break).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store


def should_respawn(task_id: str) -> bool:
    conn = store.get_db()
    task = store.get_task(conn, task_id)
    conn.close()

    if not task:
        return False

    # Task was killed or paused externally
    if task["status"] not in ("running",):
        return False

    # Check state.json for completion
    state_file = Path.home() / ".taskpilot" / task_id / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            phase = state.get("phase", "").lower()
            if phase in ("done", "completed"):
                # Mark as completed in DB
                conn = store.get_db()
                store.update_status(conn, task_id, "completed")
                conn.close()
                return False
        except (json.JSONDecodeError, KeyError):
            pass

    # Increment invocation count
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
