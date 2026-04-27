#!/usr/bin/env python3
"""CLI entry point for spawning taskpilot tasks.

Designed to be called via SSH from phone Claude:
  ssh 100.99.44.89 "python /path/to/spawner_cli.py 'task description here'"

All output is JSON — no print statements that break parsing.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store
import spawner


def main():
    parser = argparse.ArgumentParser(description="Spawn a taskpilot task")
    parser.add_argument("description", help="Task description")
    parser.add_argument("--name", help="Explicit task name (default: derived from description)")
    parser.add_argument("--plugins", help="Comma-separated plugin directory paths", default="")
    parser.add_argument("--brief", help="Path to JSON file with operating brief", default="")
    parser.add_argument("--cwd", help="Working directory for the task", default="")
    parser.add_argument("--channels", help="Comma-separated additional dev channel servers", default="")
    parser.add_argument("--dry-run", action="store_true", help="Create task without spawning")
    args = parser.parse_args()

    try:
        name = args.name or args.description[:80]
        task_id = spawner.slugify(name)
        plugins = [p.strip() for p in args.plugins.split(",") if p.strip()]

        # Load operating brief from file if provided
        operating_brief = {}
        if args.brief:
            brief_path = Path(args.brief)
            if brief_path.exists():
                operating_brief = json.loads(brief_path.read_text())

        # Auto-resolve capability plugins via nov-dependency-resolver
        capabilities = operating_brief.get("capabilities", [])
        if capabilities:
            resolved = spawner.resolve_capabilities(capabilities)
            for path in resolved:
                if path not in plugins:
                    plugins.append(path)

        conn = store.get_db()

        # Check for duplicate
        existing = store.get_task(conn, task_id)
        if existing:
            conn.close()
            print(json.dumps({
                "ok": False,
                "error": f"Task '{task_id}' already exists with status '{existing['status']}'",
            }))
            sys.exit(1)

        # Parse optional params
        cwd = args.cwd or None
        channels = [c.strip() for c in args.channels.split(",") if c.strip()]

        # Create task
        task = store.create_task(conn, task_id, name, args.description, plugins, operating_brief,
                                 cwd=cwd, channels=channels)
        conn.close()

        # Write config files
        spawner.write_task_config(task_id, name, args.description, plugins, operating_brief)

        if args.dry_run:
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "task_id": task_id,
            }))
            return

        # Spawn tmux session (~16s for startup dialogs)
        success = spawner.spawn_tmux(task_id, plugins, cwd=cwd, channels=channels, kind="task")
        if not success:
            print(json.dumps({"ok": False, "error": "Failed to launch tmux session"}))
            sys.exit(1)

        # Update status
        conn = store.get_db()
        store.update_status(conn, task_id, "running")
        store.increment_invocation(conn, task_id)
        conn.close()

        # Send initial prompt via session-bridge
        spawner.send_initial_prompt(task_id, args.description)

        # Start task relay (forwards SSE replies to phone)
        relay_script = Path(__file__).parent / "task_relay.sh"
        if relay_script.exists():
            subprocess.Popen(
                ["bash", str(relay_script), task_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "tmux_session": spawner.tmux_session_name(task_id),
            "channel_healthy": spawner.channel_healthy(task_id),
        }))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
