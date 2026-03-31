---
name: spawn
description: Spawn a long-running autonomous Claude Code session for a background task
version: 0.2.0
---

# /taskpilot:spawn

Spawn a new autonomous agent session.

## Workflow

1. **Understand the task.** Ask the user what they want done. Get a clear description.

2. **Build the operating brief.** Based on the task complexity, gather additional context:
   - **Objectives**: What are the measurable goals? (e.g., "identify 5 profitable niches", "post 3x/week")
   - **Workflows**: What ordered steps/phases should the agent follow?
   - **Success criteria**: How do we know the task is done?
   - **Boundaries**: What should the agent NOT do? (e.g., "don't spend money", "don't post without approval")
   - **Capabilities**: What capabilities does the agent need? Available:
     - `memory` — persistent knowledge across sessions
     - `scheduling` — cron-driven recurring events
     - `human-approval` — gate actions behind human confirmation
     - `notification` — alert the user
   - **Schedule**: If this is a recurring agent, what's the cadence? (cron expression)

   For simple tasks, the brief can be minimal. For long-running business agents, fill out as much as makes sense.

3. **Determine plugins needed.** Based on the task, identify which plugins the spawned session needs access to. Capabilities are auto-resolved via nov-dependency-resolver — you only need to specify plugins that aren't covered by the capability system.

4. **Create the task.** Call `create_task(name, description, plugins, operating_brief)` from the taskpilot MCP. The operating brief is a dict with keys: objectives, workflows, success_criteria, boundaries, capabilities, schedule.

5. **Spawn the task.** Call `spawn_task(task_id)`. This takes ~16 seconds to start up (trust dialog + channel initialization).

6. **Confirm.** Tell the user:
   - The task is running
   - The tmux session name (they can `tmux attach -t <name>` to watch)
   - The channel port (they can `curl -s -d 'message' http://localhost:<port>` to send messages)
   - What capabilities were resolved and loaded
   - How to check status: `/taskpilot:status`
   - How to manage: `/taskpilot:manage`
