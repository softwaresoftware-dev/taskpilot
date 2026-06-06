---
name: spawn
description: Spawn a long-running autonomous Claude Code session for a background task
version: 0.3.0
---

# /taskpilot:spawn

Spawn a new autonomous agent session. Agents run in their own tmux session,
supervised by the taskpilot daemon, and are addressable by task id through
session-bridge.

## Prerequisite

The taskpilot daemon must be running — it's the local service that owns the
spawn/kill/message lifecycle. If a tool returns "daemon is not reachable",
start it with `python daemon.py` or install it as a boot service with
`python daemon.py --install`.

## Workflow

1. **Understand the task.** Ask the user what they want done. Get a clear description.

2. **Build the operating brief.** Based on the task complexity, gather context:
   - **Objectives**: What are the measurable goals? (e.g., "identify 5 profitable niches")
   - **Workflows**: What ordered steps/phases should the agent follow?
   - **Success criteria**: How do we know the task is done?
   - **Boundaries**: What should the agent NOT do? (e.g., "don't spend money", "don't post without approval")
   - **Capabilities**: Reminders of what the agent already has, added as guidance
     sections to its CLAUDE.md (the tools come from the inherited environment, not
     resolved here):
     - `memory` — persistent knowledge across sessions
     - `human-approval` — gate actions behind human confirmation
     - `notification` — alert the user

   For simple tasks, the brief can be minimal. For long-running agents, fill out as much as makes sense.

3. **Determine plugins needed.** The spawned agent inherits the user's full
   `~/.claude` — every installed plugin and MCP is already available. Only pass
   `plugins` (dev-mode `--plugin-dir` paths) for plugins that are NOT installed.

4. **Choose model (if requested).** If the user wants a specific model, pass it as
   the `model` parameter. Valid values: `"sonnet"`, `"opus"`, `"haiku"`, or a full
   model ID. If not specified, the agent uses the default model.

5. **Define the task.** Call `define_task(name, description, plugins, operating_brief, model, cwd)`.
   The operating brief is a dict with keys: objectives, workflows, success_criteria, boundaries, capabilities.

6. **Spawn the task.** Call `spawn_task(task_id)`. This takes ~16 seconds (tmux + channel init).

7. **Confirm.** Tell the user:
   - The task is running
   - The tmux session name (they can `tmux attach -t <name>` to watch)
   - How to check status: `/taskpilot:status`
   - How to manage: `/taskpilot:manage`
