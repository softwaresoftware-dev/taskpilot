# CLAUDE.md — taskpilot

Spawn and manage long-running autonomous Claude Code sessions. Each task runs in its own tmux session, addressable through session-bridge by its task id. A long-lived supervisor daemon owns the lifecycle.

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
- A single `taskpilot-daemon.service` systemd user unit supervises all tasks

## Platform support

Linux and macOS natively (tmux dep). **Windows: via WSL2** — Claude Code
runs inside the WSL distro, taskpilot installs and behaves identically to
native Linux from inside WSL. There is no Windows-native code path because
spawning + supervising claude subprocesses currently goes through tmux;
porting that to plain `subprocess.Popen` + log-tail (instead of attach) is
on the roadmap.

The marketplace `environment` stays `{os: [linux, darwin]}` so the resolver
refuses install on native Windows — operators get a clear failure rather
than a half-working install. Inside WSL, `probe_os` returns "linux" so the
resolver accepts the install transparently.

WSL setup gotchas for taskpilot specifically:

- The dispatcher and dashboard are daemons. WSL2 shuts down when all
  shells exit — keep one open, or enable systemd in `wsl.conf` and run
  `wsl --shutdown-timeout` so units survive.
- Webhook ingress works via Cloudflare tunnel running inside WSL.
- Notifications: `notify-send` doesn't work without WSLg + a display
  server. Use `notify-slack` / `notify-email` instead.

## How It Works

1. `create_task()` writes config to `~/.taskpilot/<id>/`.
2. `spawn_task()` POSTs to the daemon's `/tasks/<id>/spawn`. The daemon launches Claude in a fresh tmux session.
3. Claude is launched with `--name <task_id>` and `SESSION_NAME=<task_id>` exported into its env. session-bridge's `channel.mjs` reads `SESSION_NAME` (along with `SESSION_NAMESPACE` and `SESSION_LABELS`) and includes them in its `/register` payload, so the mesh names the session under the task id.
4. Claude is also launched with `--settings <task_dir>/hook-settings.json` so per-task `Stop`, `Notification`, and `UserPromptSubmit` hooks fire (see "Lifecycle Hooks" below).
5. The initial task prompt is POSTed to `http://127.0.0.1:8910/sessions/<task_id>/message`.
6. External callers (taskboard "msg" button, cron schedules) send messages the same way.
7. The daemon's reconciler tick (every 60s) walks running tasks. If a service's tmux died, it respawns. If a task's tmux died, it marks crashed.
8. Task completes when the agent writes `"phase": "done"` to state.json or its final assistant message matches the completion regex; the Stop hook flips status to `completed` and the reconciler ignores completed tasks.

## Supervisor Daemon

`daemon.py` runs as a boot-persistence service on port `:8912` — a systemd user unit (`taskpilot-daemon.service`) on Linux, a launchd agent (`com.softwaresoftware.taskpilot-daemon`) on macOS. It exposes:

- `GET /health` — daemon status + supervised task count
- `GET /tasks` — list with live tmux/channel health
- `GET /tasks/<id>` — task detail + state.json
- `GET /tasks/<id>/log` — tmux pane capture
- `POST /tasks/<id>/spawn` — launch via `spawner.spawn_tmux`, send initial prompt, flip status to running
- `POST /tasks/<id>/kill` — kill tmux, clean MCPs, flip status to killed
- `POST /tasks/<id>/message` — proxy to session-bridge

Both `kind=task` and `kind=service` use the same spawn path. The kind difference is reconciler treatment, not spawn behaviour:

- `kind=service` — auto-respawned by the reconciler on tmux death
- `kind=task` — marked `crashed` on tmux death (one-shot semantics)

The MCP server (`server.py`) is a thin client — `_daemon_call()` POSTs to the daemon, falling back to in-process spawn when the daemon is unreachable so the tool works in tests and pre-daemon installs.

Install or update the boot-persistence service (systemd on Linux, launchd on macOS — auto-detected):

```bash
python3 daemon.py --install      # Linux: writes ~/.config/systemd/user/taskpilot-daemon.service. macOS: writes ~/Library/LaunchAgents/com.softwaresoftware.taskpilot-daemon.plist. Then enables + starts.
python3 daemon.py --uninstall    # stop, disable, remove
journalctl --user -u taskpilot-daemon -f
```

The reconciler interval is configurable via `TASKPILOT_RECONCILE_INTERVAL_S` (default 60s).

## Lifecycle Hooks

Spawned agents run with three Claude Code hooks registered via `--settings`:

- **`Stop`** → `hooks/on-stop.py` — fires when the assistant finishes a turn. Records `last_assistant_message`, timestamp, and session id to `state/agent.json`, then classifies and acts.
- **`Notification`** → `hooks/on-notification.py` — fires when Claude has been idle at a prompt past ~6s. Records `notification_type` (`permission_prompt` / `elicitation_dialog` / `elicitation_url_dialog`) plus message and title.
- **`UserPromptSubmit`** → `hooks/on-prompt.py` — fires when an inbound prompt arrives (mesh message or user input). Records the prompt (truncated) so received-vs-replied can be paired against the matching Stop event.

All three share `hooks/_record.py` for the read-modify-write of `agent.json` and the `events.jsonl` audit log. They no-op when `TASKPILOT_TASK_ID` is unset, which keeps them safe if the settings file is loaded outside a taskpilot context. Each hook also calls `mark_seen()` to stamp `tasks.last_seen_at = now`.

### Stop hook classify + act

`classifier.py` buckets the final assistant message:

| Bucket | Trigger | Action (in `actions.py`) |
|---|---|---|
| `resolved` | message tail matches `COMPLETION_PATTERNS` | `mark_completed_and_kill` — flips DB to `completed`, detached `tmux kill-session` |
| `question` | message tail ends in `?` | `notify_human` — appends to `escalations.jsonl`, shells out to `$TASKPILOT_NOTIFY_CMD` if set |
| `uneventful` | neither | no-op; agent stays at the prompt |

`$TASKPILOT_NOTIFY_CMD` is the user's plug point for whichever notification transport they want (Slack webhook, phone bridge, `notify-send`, etc.). The script gets `TASKPILOT_TASK_ID` and `TASKPILOT_MESSAGE` in env. Resolved bucket false-positives cause premature completion, so the regex is conservative; question bucket false-positives only cost a stray notification, so the rule is loose.

## Architecture

- Messaging routes through the session-bridge daemon at `http://127.0.0.1:8910`.
- Supervision lives in `taskpilot-daemon` at `http://127.0.0.1:8912`.
- Project-scoped MCPs from the task's `cwd/.claude/settings.json` are registered into `~/.claude.json` at launch (and cleaned up on kill via `project_mcps.json`).
- Trust dialog + channels warning auto-accepted via `tmux send-keys Enter`.

## Sandboxed $HOME

Each spawned agent runs with `HOME=~/.taskpilot/<task_id>/home/` instead of inheriting the user's daily-driver `~/.claude` environment (global CLAUDE.md, rules, every installed plugin's skills, every registered MCP). `prepare_sandbox` in `spawner.py` builds this curated $HOME:

- `~/.claude/plugins/` — symlinked to the user's real dir, so the loader can find every plugin and its marketplace metadata. Curation happens via `enabledPlugins`, not by hiding files.
- `~/.claude/settings.json` — sandbox-local. Lists `enabledPlugins` (only the curated set), carries forward `pluginConfigs` for each enabled plugin (so `CLAUDE_PLUGIN_OPTION_*` env vars inject), and `extraKnownMarketplaces`. Sensitive userConfig values live in the OS keychain and resolve automatically — the agent runs as the same OS user.
- `~/.claude/sessions/` + `~/.claude/.credentials.json` — symlinked to the user's (session-bridge scans the real sessions dir; credentials avoid a re-login).
- `~/.claude.json` — copied from the user's minus `mcpServers` and `projects`, so account/onboarding state carries forward but global MCPs and per-project history don't. `mcpServers` is then repopulated with only the task's `enabled_mcps` (see below).
- `~/.claude/projects/` — sandbox-local; transcripts isolate per agent.

Which plugins load is set by `create_task(enabled_plugins=[...])` — a list of marketplace keys (e.g. `liteframe@softwaresoftware-plugins`). `session-bridge` and `taskpilot` are always enabled (channel + lifecycle hooks); everything else stays installed but inert unless listed. This is what lets a caller (llm-dispatcher, plugin-tester) request a specific plugin set per task. Note `enabled_plugins` is distinct from `plugins`, which is dev-mode `--plugin-dir` filesystem paths and loads regardless of `enabledPlugins`.

Which MCP servers the agent gets is set by `create_task(enabled_mcps=[...])` — a list of MCP server names (e.g. `["gmail-organizer", "slack"]`). Each name is resolved against the user's real `~/.claude.json` `mcpServers` and copied verbatim into the sandbox's. The sandbox starts with zero MCP servers — the user's globals never leak in — so a task only gets the servers its caller declares. Names with no match in the user's config are skipped.

## Data

- Database: `~/.taskpilot/taskpilot.db` (SQLite with WAL mode)
- Task configs: `~/.taskpilot/<task_id>/` (CLAUDE.md, state.json, brief.json, hook-settings.json)
- Daemon journal: `journalctl --user -u taskpilot-daemon`

## Development

```bash
pip install "mcp[cli]" "fastapi>=0.115" "uvicorn[standard]>=0.30"
python server.py                # run MCP server
python daemon.py                # run supervisor daemon (foreground; --install to register systemd unit)
make test                       # run tests
```

Install as plugin:
```bash
claude --plugin-dir /home/thatcher/projects/softwaresoftware/projects/plugins/providers/taskpilot
```

## MCP Tools

- `create_task(name, description, plugins?, operating_brief?, model?, kind?, host?, enabled_plugins?, enabled_mcps?)` — create task config + allocate port. kind="service" for reboot-persistent agents. host="<peer>" to launch the agent on a remote mesh host (forwards spawn to that peer's session-bridge `/spawn`). `enabled_plugins` is a list of installed-plugin marketplace keys to enable in the task's sandbox; `enabled_mcps` is a list of MCP server names to inject from the user's `~/.claude.json` (both see "Sandboxed $HOME" below); `plugins` is the separate dev-mode `--plugin-dir` path list.
- `spawn_task(task_id)` — launch tmux session (~16s startup). When the task carries `host` and that host is not self, forwards to the peer's `POST /spawn` instead.
- `list_tasks(status?)` — list all tasks with live health
- `get_task(task_id)` — full detail + state.json
- `send_message(task_id, message)` — POST to channel
- `kill_task(task_id)` — kill tmux + clean up
- `get_task_log(task_id, lines?)` — capture tmux pane output
- `schedule_task(name, plugin, skill, interval, enabled?)` — create/update a cron schedule
- `list_scheduled_tasks()` — list schedules for current task
- `remove_scheduled_task(name)` — remove a schedule and its crontab entry

## Operating Brief

The `operating_brief` parameter to `create_task` accepts a dict with:

| Key | Type | Purpose |
|-----|------|---------|
| `objectives` | list[str] | Measurable goals |
| `workflows` | list[str] | Ordered phases/steps |
| `success_criteria` | list[str] | Completion conditions |
| `boundaries` | list[str] | What NOT to do |
| `capabilities` | list[str] | Required capabilities (auto-resolved via softwaresoftware) |
| `schedule` | str | Cron expression for recurring agents (scheduling is built-in) |

Capabilities declared in the brief are automatically resolved to provider plugins at task creation time. The agent's CLAUDE.md is dynamically generated with sections for each declared capability.

Environment variable `TASKPILOT_TASK_ID` is exported in the tmux session so capability plugins can scope their storage per-task.
