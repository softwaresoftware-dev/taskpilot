#!/usr/bin/env python3
"""Stop hook for taskpilot agents.

Fires when the assistant finishes a turn. Records the final assistant message
and timestamp to state/agent.json so rotation.py and external tools can
classify the agent's last state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _record import now_iso, read_event, task_id, write_record


def main() -> int:
    tid = task_id()
    if not tid:
        return 0

    event = read_event()
    if event is None:
        return 0

    record = {
        "received_at": now_iso(),
        "stop_hook_active": event.get("stop_hook_active", False),
        "last_assistant_message": event.get("last_assistant_message", ""),
        "session_id": event.get("session_id"),
    }
    write_record(tid, "last_stop", record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
