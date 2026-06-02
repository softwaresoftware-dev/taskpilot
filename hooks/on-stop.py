#!/usr/bin/env python3
"""Stop hook for taskpilot agents.

Fires when the assistant finishes a turn. Responsibilities:

1. Record the final assistant message + timestamp to state/agent.json.
2. Stamp last activity (last_seen_at) so the idle clock resets each turn.

NOTE: this hook used to run an LLM/regex classifier and KILL the agent when its
message "looked done." That was removed — completion is no longer inferred from
prose. Going idle just lets the reconciler recycle the agent to 'dormant' (it
wakes on the next message). Completion, if ever needed, is an explicit signal,
not a guess. See daemon.py reconcile + wake-on-message.
"""

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent
PLUGIN_ROOT = HOOKS_DIR.parent

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(PLUGIN_ROOT))

from _record import mark_seen, now_iso, read_event, task_id, write_record


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
    # No classification, no completion-kill. Idle is handled by the reconciler
    # (recycle to 'dormant'), not by guessing the agent is "done" from its words.
    return 0


if __name__ == "__main__":
    sys.exit(main())
