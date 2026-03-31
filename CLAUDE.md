# CLAUDE.md — taskpilot

Spawn and manage long-running autonomous Claude Code sessions. Each task runs in its own tmux session with a channel MCP for real-time communication.

## Quick Reference

| Command | What it does |
|---------|-------------|
| `/taskpilot:spawn` | Create and launch a new autonomous task |
| `/taskpilot:status` | Dashboard of all tasks with health status |
| `/taskpilot:manage` | Send messages, view logs, kill tasks |

## Stack

- Python 3.11+, FastMCP, SQLite
- Node.js (channel MCP server)
- tmux (session management)

## How It Works

1. `create_task()` writes config to `~/.taskpilot/<id>/`, allocates a port, registers channel MCP in `~/.claude.json`
2. `spawn_task()` launches a tmux session running Claude with the channel loaded
3. The initial task prompt is POSTed to the channel after startup
4. The agent works autonomously, writing state.json after each major action
5. Messages flow via HTTP POST to the channel port; replies come back on SSE `/events`
6. On crash, the while-loop in tmux respawns via `rotation.py`
7. Task completes when the agent writes `"phase": "done"` to state.json

## Architecture

- Channel MCP servers registered in `~/.claude.json` (NOT settings.json)
- Node path must be absolute (nvm not in MCP subprocess PATH)
- Trust dialog + channels warning auto-accepted via `tmux send-keys Enter`
- Each task gets a unique port (9100+)

## Data

- Database: `~/.taskpilot/taskpilot.db` (SQLite with WAL mode)
- Task configs: `~/.taskpilot/<task_id>/` (CLAUDE.md, state.json, brief.json)

## Development

```bash
pip install "mcp[cli]"
python server.py                # run MCP server
make test                       # run tests
```

Install as plugin:
```bash
claude --plugin-dir /home/thatcher/projects/nov/projects/plugins/taskpilot
```

## MCP Tools

- `create_task(name, description, plugins?, operating_brief?)` — create task config + allocate port
- `spawn_task(task_id)` — launch tmux session (~16s startup)
- `list_tasks(status?)` — list all tasks with live health
- `get_task(task_id)` — full detail + state.json
- `send_message(task_id, message)` — POST to channel
- `kill_task(task_id)` — kill tmux + clean up
- `get_task_log(task_id, lines?)` — capture tmux pane output

## Operating Brief

The `operating_brief` parameter to `create_task` accepts a dict with:

| Key | Type | Purpose |
|-----|------|---------|
| `objectives` | list[str] | Measurable goals |
| `workflows` | list[str] | Ordered phases/steps |
| `success_criteria` | list[str] | Completion conditions |
| `boundaries` | list[str] | What NOT to do |
| `capabilities` | list[str] | Required capabilities (auto-resolved via nov-dependency-resolver) |
| `schedule` | str | Cron expression for recurring agents |

Capabilities declared in the brief are automatically resolved to provider plugins via nov-dependency-resolver at task creation time. The agent's CLAUDE.md is dynamically generated with sections for each declared capability.

Environment variable `TASKPILOT_TASK_ID` is exported in the tmux session so capability plugins can scope their storage per-task.
