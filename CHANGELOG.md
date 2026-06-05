# Changelog

All notable changes to taskpilot.

## 0.12.1 — 2026-06-04

### Removed

- **The completion-classifier machinery (dead code).** The `4f12e15` lifecycle
  change already stopped wiring the Stop hook to a prose-classifier that
  inferred completion and killed the agent; this release deletes the now-orphaned
  code: `classifier.py` (the `claude -p` Haiku judge), `actions.py`
  (`mark_completed_and_kill` + `notify_human` + the pre-kill pane.log flush),
  and their test suites (`test_classifier.py`, `test_actions.py`,
  `test_actions_pane_log.py`).
- **The pane.log `pane.log.attached` sentinel.** It existed only so the
  completion flush could pick the steady-vs-legacy path. With that path gone the
  sentinel had no reader, so `PANE_LOG_SENTINEL_NAME`, `pane_log_sentinel()`, and
  the sentinel create/unlink in `_setup_pane_log_capture` are removed. Live
  pane.log capture (pipe-pane tee, invocation separators, size cap) is unchanged
  — killed/recycled tasks still get their scrollback via the live tee's EOF flush.

### Changed

- The real-agent e2e (`test_e2e_real_agent.py`) no longer asserts a `classify→act`
  completion transition (that behavior no longer exists); it now verifies spawn,
  hook dispatch, and Stop-hook recording only.
- Docs (`CLAUDE.md`) updated: completion is never inferred from prose; idle agents
  are recycled to `dormant` and wake on the next message.

## 0.12.0 — 2026-06-04

### Removed

- **Sandboxed `$HOME` for spawned agents (and the per-task curation it enabled).** Agents no longer run under a redirected `HOME` with a curated `~/.claude`. Each spawned agent now inherits the user's real `~/.claude` environment — global `CLAUDE.md`, rules, every installed plugin's skills, and every registered MCP server. Dropped: `prepare_sandbox`/`sandbox_home`/`_user_login_path` in `spawner.py`, the `HOME`/`PATH`/`TASKPILOT_HOME` env exports, and the `TASKPILOT_HOME` fallbacks in `store.py`, `actions.py`, `classifier.py`, and `hooks/_record.py`.
- **`enabled_plugins` and `enabled_mcps` (breaking).** These per-task curation parameters were implemented entirely through the sandbox, so they are removed from `create_task` (MCP tool + `store.create_task`), the `--enabled-plugins`/`--enabled-mcps` CLI flags, and the `tasks` table columns. A spawned agent gets whatever plugins/MCPs the user has enabled globally.

### Changed

- `capture_session_id(task_id, cwd=None)` now reads transcripts from the real `~/.claude/projects/<encoded-cwd>/` (cwd with non-alphanumerics replaced by `-`) instead of the sandbox home.
- The dispatcher provider's `spawn_helper.py` no longer passes `--enabled-plugins`/`--enabled-mcps` to the taskpilot spawner.

## 0.10.0 — 2026-05-17

### Added

- **Per-task MCP server injection via `enabled_mcps`.** `create_task` accepts a new `enabled_mcps` list of MCP server names (e.g. `["gmail-organizer", "slack"]`). Each name is resolved against the user's real `~/.claude.json` `mcpServers` and copied verbatim into the task's sandbox. The sandbox otherwise has zero MCP servers — the user's globals never leak in — so a task gets exactly the servers its caller declares. Names with no match are skipped. CLI: `--enabled-mcps`. This completes the v0.9.0 sandbox story: a caller now fully defines the agent's environment (plugins via `enabled_plugins`, MCP servers via `enabled_mcps`).

### Changed

- `prepare_sandbox` takes `enabled_mcps: list[str]` (server names, resolved internally) in place of the unused `declared_mcps: dict` parameter.

## 0.9.0 — 2026-05-15

### Added

- **Sandboxed `$HOME` for spawned agents.** Each agent runs with `HOME` set to its own task directory instead of inheriting the user's daily-driver `~/.claude` environment. `prepare_sandbox` builds a curated home: the user's `plugins/`, `sessions/`, and `.credentials.json` are symlinked in; `settings.json` is sandbox-local with a curated `enabledPlugins`; `.claude.json` carries account/onboarding state minus the user's global `mcpServers` and `projects`. This cut the context floor for a minimal agent from ~47k to ~33k tokens.
- **Per-task plugin curation via `enabled_plugins`.** `create_task` accepts a new `enabled_plugins` list of installed-plugin marketplace keys (e.g. `liteframe@softwaresoftware-plugins`) to enable in the task's sandbox. `session-bridge` and `taskpilot` are always enabled; everything else stays installed but inert, so its skills and tools never load into the agent's context. Lets a caller request a specific plugin set per task. CLI: `--enabled-plugins`.
- **`pluginConfigs` carry-forward.** The sandbox `settings.json` now carries forward each enabled plugin's `pluginConfigs` entry (and `extraKnownMarketplaces`) from the user's real settings, so an enabled plugin's `CLAUDE_PLUGIN_OPTION_*` env vars still inject. Previously any plugin beyond the two defaults would have come up unconfigured.

### Fixed

- **Personal skills no longer leak into the sandbox.** Claude Code discovers project `.claude/` config (skills, rules, `CLAUDE.md`) by walking up the directory tree from cwd, stopping at `$HOME`. The sandbox previously ran the agent with cwd at the *parent* of `$HOME`, so the walk escaped to the real `/home/<user>/.claude/` and pulled in personal skills. `sandbox_home` is now the task directory itself, so `HOME == cwd` and the walk terminates inside the sandbox.

## 0.8.0 — 2026-05-06

### Added

- **Persistent pane logs.** Tmux pane output is now teed to `~/.taskpilot/<task_id>/pane.log` via `tmux pipe-pane`. The file survives task completion, kill, and reconciler respawn — so `get_task_log` (and downstream consumers like taskboard) can read agent history after the tmux session is gone.
- **Three-tier `get_task_log` read**: live tmux pane (`source: "tmux"`) → persisted `pane.log` (`source: "pane.log"`) → 404. The new `source` field in the response indicates which tier served the call. Existing callers reading only `output` are unaffected.
- **Pre-kill flush** in `actions.mark_completed_and_kill`. Steady path (pipe-pane was attached this invocation) toggles off the tee, drains briefly, writes a `=== completed ===` separator, then runs the detached kill. Legacy path (no sentinel — task pre-dates the upgrade or pipe-pane install failed) does a `tmux capture-pane -p -S -` into `pane.log` before the kill, so existing tasks completing through the upgrade boundary still get their content recovered.
- **Soft size cap** at spawn boundaries. Default 10 MB per `pane.log`; head-truncates to last 5 MB plus a marker on overflow. Configurable via `TASKPILOT_PANE_LOG_MAX_BYTES` (minimum 4 KB).
- **Invocation separators** in `pane.log`. Each spawn appends `=== taskpilot invocation N at <iso> reason=start|respawn ===` so users grepping accumulated logs can tell where each invocation begins.
- **Sentinel file** `pane.log.attached` in each task dir. Marks pipe-pane successful attach this invocation; the discriminator for the steady-vs-legacy completion path. Removed automatically by `destroy_task`'s rmtree.

### Changed

- `tmux capture-pane` calls now pin the target as `<session>:0.0` (defensive against future window additions to the spawn flow).
- `mark_completed_and_kill` uses `spawner.tmux_session_name(task_id)` rather than assuming `task_id == session` directly.
- The 404 message from `get_log` is now `"no log available"` (was `"Failed to capture pane"`); semantically equivalent for callers.

### Limitations

- **Long-running services that don't crash** will grow `pane.log` unboundedly between respawns. The size cap only enforces at spawn boundaries. Reconciler-side mid-flight rotation is tracked in `TODO_v0.8.1.md`.
- **Remote tasks** (`host` set, spawn forwarded to a peer host) get `pane.log` on the peer; local `get_task_log` for remote tasks 404s. This is unchanged from prior behavior.
- **Windows is unsupported** — relies on POSIX tmux, `stdbuf` (optional), and POSIX file modes. Existing taskpilot constraint, unchanged.

### Migration

- No DB schema changes. Uses existing `invocation_count` column.
- Existing running tasks pick up `pane.log` capture on their next spawn (`kind=service` reconciler respawn) or completion (`kind=task` going through the legacy path).
- No data is lost.

## 0.7.x and earlier

See git history.
