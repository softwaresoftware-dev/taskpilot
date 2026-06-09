# CLAUDE.md — taskpilot

A local service that exposes an HTTP API for running long-running autonomous
Claude Code agents. The `taskpilot-daemon` is the product: a boot-persistent
process on `:8912` that spawns each agent in its own tmux session and owns its
spawn/kill/message lifecycle. The MCP server is a thin client over that API so
a Claude session can drive it. Agents are addressable by task id through
session-bridge.

**Role in the mindframe stack:** taskpilot is the **Agent runtime** layer. It
spawns each agent in tmux and delivers the starter prompt and every later
message over the **Mesh** (session-bridge `:8910/sessions/<id>/message`), never
by typing into the pane. Callers spawn through `POST :8912/tasks/create_and_spawn`.
It is a standalone provider; mindframe is one consumer.

## Quick Reference

| Command | What it does |
|---------|-------------|
| `/taskpilot:spawn` | Create and launch a new autonomous task |
| `/taskpilot:status` | Dashboard of all tasks with health status |
| `/taskpilot:manage` | Send messages, view logs, kill tasks |

## Stack

- Python 3.11+, FastMCP, FastAPI, SQLite
- tmux (session management)
- session-bridge (message routing)
- A single `taskpilot-daemon` boot service, installed via the daemon capability (daemon-manager: systemd user unit on Linux, launchd agent on macOS)

## Platform support

Linux and macOS natively (tmux dep). **Windows: via WSL2** — Claude Code runs
inside the WSL distro, taskpilot installs and behaves identically to native
Linux from inside WSL. There is no Windows-native code path because spawning
claude subprocesses currently goes through tmux.

The marketplace `environment` stays `{os: [linux, darwin]}` so the resolver
refuses install on native Windows. Inside WSL, `probe_os` returns "linux" so
the resolver accepts the install transparently.

## How It Works

1. `define_task()` writes config to `~/.taskpilot/<id>/` and a row to the DB. This is the one MCP call that runs in-process — everything else goes through the daemon.
2. `spawn_task()` POSTs to the daemon's `/tasks/<id>/spawn`. The daemon launches Claude in a fresh tmux session via `spawner.spawn_tmux`.
3. Claude is launched with `--name <task_id>` and `SESSION_NAME=<task_id>` exported into its env. session-bridge's `channel.mjs` reads `SESSION_NAME` (and `SESSION_NAMESPACE`) and includes them in its `/register` payload, so the mesh names the session under the task id.
4. The initial task prompt is POSTed to `http://127.0.0.1:8910/sessions/<task_id>/message`.
5. External callers (e.g. the mindframe dashboard's message box) send messages the same way.

The daemon is **reactive**: it acts on API calls, not on a background timer.
There is no reconciler, no auto-respawn, and no completion inference. A task's
status reflects the last lifecycle call (`pending` → `running` → `killed`).
Liveness (is the agent's tmux still alive) is computed on demand whenever a
task is listed or fetched (`tmux_alive`, `channel_healthy`). A task whose tmux
has died still shows `status: running` with `tmux_alive: false` — re-launch it
by killing it first (clears the status) then spawning again.

## Supervisor Daemon

`daemon.py` runs as a boot-persistence service on port `:8912`. As of 0.14 the
boot unit is **installed and managed by the daemon capability (daemon-manager
≥ 1.5.0)**, not self-rendered: a systemd user unit (`taskpilot-daemon.service`)
on Linux, a launchd agent (`com.claude.daemon.taskpilot-daemon`) on macOS.
daemon-manager emits `KillMode=process` / `AbandonProcessGroup` (detached tmux
agents survive a daemon restart) and `After=`/`Wants=session-bridge.service`
ordering — the directives that previously forced taskpilot to self-manage.
Registering through daemon-manager also means a plugin update auto-restarts the
daemon onto new code (version-drift sync) instead of running stale code until a
manual restart. It exposes:

- `GET /health` — daemon status + running/total task counts
- `GET /tasks` — list with live tmux/channel health
- `GET /tasks/<id>` — task detail + state.json
- `POST /tasks/<id>/spawn` — launch via `spawner.spawn_tmux`, send initial prompt, flip status to running
- `POST /tasks/<id>/kill` — kill tmux, clean project MCPs, flip status to killed
- `POST /tasks/<id>/message` — proxy to session-bridge
- `POST /tasks/create_and_spawn` — define + spawn in one call, for event-driven callers (e.g. the dispatcher's `spawn:<recipe>` path) that hold no prior task row. Body: `{description, name?, cwd?, model?, brief?}`. The task_id is slugified from `name` (or the description); idempotent for a caller that predicts the id. No MCP-tool equivalent — HTTP only.

The MCP server (`server.py`) is a thin client: `_daemon_call()` POSTs to the
daemon and there is **no in-process fallback** — if the daemon is down, the
tools return a clear "daemon is not reachable" error. The daemon is the
service; it must be running.

Install or repair the boot-persistence service from Claude Code:

```
/taskpilot:setup
```

The setup skill registers the daemon through the daemon capability
(`daemon_start` + `daemon_install_autostart` with `kill_mode="process"`,
`after`/`wants=["session-bridge.service"]`) and verifies `/health`. The old
`daemon.py --install` / `--uninstall` self-render path is retired — those flags
now just point back to `/taskpilot:setup`. For dev, run the daemon in the
foreground with `python daemon.py`. Tail logs with
`journalctl --user -u taskpilot-daemon -f` (Linux).

## Architecture

- Messaging routes through the session-bridge daemon at `http://127.0.0.1:8910`.
- The lifecycle service lives in `taskpilot-daemon` at `http://127.0.0.1:8912`.
- Project-scoped MCPs from the task's `cwd/.claude/settings.json` are registered into `~/.claude.json` at launch (and cleaned up on kill via `project_mcps.json`).
- Trust dialog + channels warning auto-accepted via `tmux send-keys Enter`.

## Agent environment

Each spawned agent inherits the user's real `~/.claude` environment — global
`CLAUDE.md`, rules, every installed plugin's skills, and every registered MCP
server. It runs with the user's real `$HOME` (no isolation): the agent is the
same OS user with the same toolchain and credentials. Per-task context comes
from the `CLAUDE.md` that `write_task_config` drops at the task dir (the
agent's cwd).

## Data

- Database: `~/.taskpilot/taskpilot.db` (SQLite with WAL mode)
- Task configs: `~/.taskpilot/<task_id>/` (CLAUDE.md, state.json, brief.json, prompt.txt)
- Daemon journal: `journalctl --user -u taskpilot-daemon`

## Development

```bash
pip install "mcp[cli]" "fastapi>=0.115" "uvicorn[standard]>=0.30"
python server.py                # run MCP server
python daemon.py                # run supervisor daemon (foreground; --install to register the boot service)
make daemon-status              # curl the daemon's /health
```

There is no automated test suite — it was retired with the feature pare-down.
Sanity-check a change by importing the modules
(`uv run python -c "import store, spawner, server, daemon"`) and, for the spawn
path, doing a real `define_task` → `spawn_task` → `send_message` → `kill_task`
against a running daemon + session-bridge.

Install as plugin:
```bash
claude --plugin-dir /home/thatcher/projects/softwaresoftware/projects/plugins/providers/taskpilot
```

## MCP Tools

- `define_task(name, description, plugins?, operating_brief?, model?, cwd?)` — write task config + allocate port (does not launch). `plugins` is a list of dev-mode `--plugin-dir` paths, only for plugins NOT already installed (installed ones are inherited). Runs in-process (writes the DB row + config files).
- `spawn_task(task_id)` — launch tmux session via the daemon (~16s startup)
- `list_tasks(status?)` — list all tasks with live health
- `get_task(task_id)` — full detail + state.json
- `send_message(task_id, message)` — POST to channel via the daemon
- `kill_task(task_id)` — kill tmux + clean up via the daemon

To watch an agent's live output, `tmux attach -t <task_id>`.

## Operating Brief

The `operating_brief` parameter to `define_task` accepts a dict with:

| Key | Type | Purpose |
|-----|------|---------|
| `objectives` | list[str] | Measurable goals |
| `workflows` | list[str] | Ordered phases/steps |
| `success_criteria` | list[str] | Completion conditions |
| `boundaries` | list[str] | What NOT to do |
| `capabilities` | list[str] | Capabilities to remind the agent it has (e.g. `memory`, `human-approval`) |

Capabilities are **documentation only**: each one adds a guidance section to
the agent's generated CLAUDE.md (e.g. "you have memory available, use it").
The tools themselves are not resolved or installed here — the spawned agent
inherits the user's full `~/.claude`, so every installed plugin and MCP is
already present. taskpilot has no runtime dependency on softwaresoftware.

Environment variable `TASKPILOT_TASK_ID` is exported in the tmux session so
capability plugins can scope their storage per-task.

## What this does NOT do (intentionally pared down)

These were removed to keep taskpilot to its irreducible core. If you need one,
it lived in git history before v0.13.0:

- **Scheduling / cron** — no `schedule_task` family. Drive recurrence from an external scheduler that POSTs a message to the agent's channel.
- **Remote / mesh spawn** — no `host=` forwarding. The service runs agents on its own machine only.
- **`kind=service` + auto-respawn** — every task is one-shot. No reconciler brings a dead agent back.
- **Idle dormancy / resume-on-wake** — no lifecycle hooks, no `--resume` wake path.
- **Log reads** — no `get_task_log` tool and no `pane.log`. To watch an agent, `tmux attach -t <task_id>`.
- **Capability → plugin resolution** — `capabilities` in the brief are doc nudges only; installed plugins/MCPs come from the inherited `~/.claude`. No runtime softwaresoftware dependency.
- **Extra dev channels** — the agent gets exactly one channel (session-bridge). No `channels` param, no channel-resolution validation.
- **`destroy_task` / `respawn_task`** — kill is the only teardown. (Note: a killed task's DB row persists, so re-creating a task with the same name returns an "already exists" error. Pick a new name, or clear the row from `~/.taskpilot/taskpilot.db`.)
