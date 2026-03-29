---
name: manage
description: Send messages to, pause, resume, or kill a running task agent
version: 0.1.0
---

# /taskpilot:manage

Manage a running task.

## Workflow

1. Call `list_tasks()` to show running tasks.
2. Ask the user which task and what action:
   - **message** — Send a message to the task. Call `send_message(task_id, message)`.
   - **view log** — Call `get_task_log(task_id)` to see recent tmux output.
   - **view state** — Call `get_task(task_id)` to read state.json.
   - **kill** — Call `kill_task(task_id)`.
3. Report the result.
