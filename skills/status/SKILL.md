---
name: status
description: Dashboard showing all taskpilot tasks with status and health
version: 0.2.0
---

# /taskpilot:status

Show the status dashboard for all tasks.

## Workflow

1. Call `list_tasks()` to get all tasks.
2. Present a table showing: task_id, name, status, port, tmux_alive, channel_healthy.
3. For running tasks, call `get_task(task_id)` to show their current state.json.
4. Flag any task that is status="running" but tmux_alive=false — its agent
   process has exited (the daemon is reactive and won't auto-restart it). To
   re-launch it, `kill_task(task_id)` first (clears the stale running status),
   then `spawn_task(task_id)`.

If `list_tasks()` returns "daemon is not reachable", the taskpilot daemon isn't
running — start it with `python daemon.py` (or `python daemon.py --install`).
