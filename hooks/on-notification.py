#!/usr/bin/env python3
"""Notification hook for taskpilot agents.

Fires when Claude has been idle at a prompt past the interaction threshold
(~6s). The notification_type field tells us why:

  permission_prompt        — tool-permission dialog
  elicitation_dialog       — MCP server requesting structured input
  elicitation_url_dialog   — MCP server asking the user to visit a URL

For taskpilot agents launched with --dangerously-skip-permissions, the
permission_prompt case is rare (most prompts auto-approve before the hook
fires). The elicitation cases are the practically interesting signals.
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
        "notification_type": event.get("notification_type"),
        "message": event.get("message"),
        "title": event.get("title"),
        "session_id": event.get("session_id"),
    }
    write_record(tid, "last_notification", record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
