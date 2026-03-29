---
name: spawn
description: Spawn a long-running autonomous Claude Code session for a background task
version: 0.1.0
---

# /taskpilot:spawn

Spawn a new autonomous agent session.

## Workflow

1. **Understand the task.** Ask the user what they want done. Get a clear description.

2. **Determine plugins needed.** Based on the task, identify which plugins the spawned session needs access to. Common plugins:
   - Browser automation: `/home/thatcher/projects/nov/projects/plugins/cardwatch` or browser-bridge
   - Notifications: handled by conversation capability (no separate plugin needed)

   Ask the user if they want specific plugins loaded. If unsure, start with no extra plugins.

3. **Create the task.** Call `create_task(name, description, plugins)` from the taskpilot MCP.

4. **Spawn the task.** Call `spawn_task(task_id)`. This takes ~16 seconds to start up (trust dialog + channel initialization).

5. **Confirm.** Tell the user:
   - The task is running
   - The tmux session name (they can `tmux attach -t <name>` to watch)
   - The channel port (they can `curl -s -d 'message' http://localhost:<port>` to send messages)
   - How to check status: `/taskpilot:status`
   - How to manage: `/taskpilot:manage`
