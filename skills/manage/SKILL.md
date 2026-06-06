---
name: manage
description: Send messages to, inspect, or kill a running task agent
version: 0.3.0
---

# /taskpilot:manage

Manage a running task.

## Workflow

1. Call `list_tasks()` to show tasks.
2. Ask the user which task and what action:
   - **message** — Send a message to the task. Call `send_message(task_id, message)`.
   - **view state** — Call `get_task(task_id)` to read state.json.
   - **watch live** — Tell the user to `tmux attach -t <task_id>` (read-only: `tmux attach -t <task_id> -r`).
   - **kill** — Call `kill_task(task_id)`.
3. Report the result.

If any tool returns "daemon is not reachable", the taskpilot daemon isn't
running — start it with `python daemon.py` (or `python daemon.py --install`).
