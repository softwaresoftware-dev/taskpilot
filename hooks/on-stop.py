#!/usr/bin/env python3
"""Stop hook for taskpilot agents.

Fires when the assistant finishes a turn. Two responsibilities:

1. Record the final assistant message + timestamp to state/agent.json.
2. Classify the message and act:
     resolved   → mark task completed in the DB and tear down tmux.
     question   → log an escalation and (if configured) fire a notification.
     uneventful → do nothing; agent stays at the prompt for further input.
"""

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent
PLUGIN_ROOT = HOOKS_DIR.parent

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(PLUGIN_ROOT))

from _record import mark_seen, now_iso, read_event, task_id, write_record
import actions
import classifier


def main() -> int:
    tid = task_id()
    if not tid:
        return 0

    event = read_event()
    if event is None:
        return 0

    message = event.get("last_assistant_message", "") or ""

    record = {
        "received_at": now_iso(),
        "stop_hook_active": event.get("stop_hook_active", False),
        "last_assistant_message": message,
        "session_id": event.get("session_id"),
    }
    write_record(tid, "last_stop", record)
    mark_seen(tid)

    bucket = classifier.classify(message)
    if bucket == "resolved":
        actions.mark_completed_and_kill(tid)
    elif bucket == "question":
        actions.notify_human(tid, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
