---
name: status
description: Dashboard showing all running taskpilot tasks with status and health
version: 0.1.0
---

# /taskpilot:status

Show the status dashboard for all tasks.

## Workflow

1. Call `list_tasks()` to get all tasks.
2. Present a table showing: task_id, name, status, port, tmux_alive, channel_healthy.
3. For running tasks, call `get_task(task_id)` to show their current state.json.
4. Flag any tasks that are status="running" but tmux_alive=false (crashed, needs respawn).
