#!/usr/bin/env python3
"""UserPromptSubmit hook for taskpilot agents.

Fires when an inbound message arrives — either a user-typed prompt or, more
commonly for service agents like the librarian, a mesh message routed in via
session-bridge. Records the prompt to events.jsonl so we can pair "query
received" with the matching Stop "reply sent" event.

This is the librarian's per-query log. Without it, there's no way to tell a
silently-dropped query from a query that was answered — only the reply side
was being recorded.

The recorded prompt is truncated to PROMPT_TRUNC chars to keep the log
greppable; the full payload is already in claude's transcript file.
"""

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent
PLUGIN_ROOT = HOOKS_DIR.parent

sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(PLUGIN_ROOT))

from _record import mark_seen, now_iso, read_event, task_id, write_record

PROMPT_TRUNC = 2000


def main() -> int:
    tid = task_id()
    if not tid:
        return 0

    event = read_event()
    if event is None:
        return 0

    prompt = event.get("prompt", "") or ""
    if len(prompt) > PROMPT_TRUNC:
        prompt = prompt[:PROMPT_TRUNC] + "…"

    record = {
        "received_at": now_iso(),
        "prompt": prompt,
        "session_id": event.get("session_id"),
    }
    write_record(tid, "last_prompt", record)
    mark_seen(tid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
