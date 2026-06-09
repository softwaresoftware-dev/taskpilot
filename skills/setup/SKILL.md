---
name: setup
description: Install and start the taskpilot supervisor daemon as a reboot-persistent service via the daemon capability. Use when asked to "set up taskpilot", "install the taskpilot daemon", "start taskpilot", or after first install when the taskpilot MCP reports "daemon is not reachable".
---

# taskpilot setup

Bring up the `taskpilot-daemon` on this machine and make it survive reboot. The
daemon owns the spawn/kill/message lifecycle on `127.0.0.1:8912`; the taskpilot
MCP tools are a thin client over it, so without the daemon every tool returns
"daemon is not reachable".

The boot service is installed through the **daemon capability** (daemon-manager),
not by `daemon.py --install`. Registering this way is what gets taskpilot
auto-restarted when its plugin is updated, instead of silently running stale code.

## Prerequisites

- `uv` and `tmux` on PATH (taskpilot's declared `binary` requirements).
- The **session-mesh** must be up first — taskpilot delivers every prompt over
  session-bridge. If the bridge daemon isn't running, set it up first (use an
  available skill or tool for the session-mesh capability), then continue.

## Workflow

1. **Locate the plugin root.** Set `ROOT="${CLAUDE_PLUGIN_ROOT}"`. The daemon is
   launched with `uv run --directory "$ROOT" python daemon.py`, which resolves
   the plugin's own venv (mcp/fastapi/uvicorn). Confirm `$ROOT/daemon.py` exists.

2. **Clear any old hand-rolled unit.** taskpilot < 0.14 wrote its own
   `taskpilot-daemon.service`. It shares the daemon-manager unit name, so it is
   overwritten in place — but stop the old one first so the port is free and the
   old supervision is cleared. Remove the existing autostart for daemon
   `taskpilot-daemon` (use an available daemon-capability tool to uninstall
   autostart). This is a no-op on a fresh install.

3. **Start the daemon.** Register and start it through the daemon capability —
   start a daemon named `taskpilot-daemon` running `uv` with args
   `["run", "--directory", "$ROOT", "python", "daemon.py"]` and cwd `$ROOT`.
   Use an available daemon-capability tool. Confirm the status is `started` or
   `already_running`. (The daemon listens on TCP `:8912`, not a Unix socket, so
   an "IPC not reachable yet" note is expected and harmless — verify health in
   step 5 instead.)

4. **Install autostart with the lifecycle directives.** Install boot autostart
   for daemon `taskpilot-daemon` through the daemon capability, passing:
   - `kill_mode = "process"` — so restarting the daemon does **not** kill the
     detached tmux agents it spawned (they survive and are re-adopted).
   - `after = ["session-bridge.service"]` — order taskpilot behind the mesh.
   - `wants = ["session-bridge.service"]` — soft dependency on the mesh.

   Use an available daemon-capability tool. Confirm the status is `installed`.
   If it is `installed_not_loaded`, surface the error and stop.

5. **Verify health.**

   ```bash
   curl -sf http://127.0.0.1:8912/health
   ```

   Expect a 200 with running/total task counts. If the process is up but
   `/health` fails, on Linux check `systemctl --user status taskpilot-daemon`
   and `journalctl --user -u taskpilot-daemon -n 50`; on macOS check
   `~/.claude/daemons/taskpilot-daemon.err.log`.

6. **Confirm it's managed.** List managed daemons via the daemon capability and
   confirm `taskpilot-daemon` appears with autostart configured. From now on,
   when the taskpilot plugin updates, the daemon-manager version-drift sync
   restarts it onto the new code automatically.

## Notes

- **Windows:** native Windows isn't supported (spawning goes through tmux). Run
  taskpilot inside WSL2, where setup behaves identically to Linux.
- **Dev / foreground:** to run the daemon by hand without boot persistence,
  `python daemon.py` in the plugin root. `daemon.py --install` is retired — it
  now just points back here.
