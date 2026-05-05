#!/usr/bin/env python3
"""Liveness reconciler for taskpilot tasks.

Run periodically (systemd user timer, every 60s by default). For every task
with status='running', verify the tmux session is alive. If it isn't, flip
status to 'crashed', record a short reason in last_error, and append a
'liveness_crash' record to the task's events.jsonl so taskboard packs and
ad-hoc grep both have a trail.

This catches the silent-death class of failure: claude OOMs, tmux dies, the
host reboots without recovery, etc. The DB used to lie indefinitely in those
cases — status stayed 'running' until something else touched the row.

Exit code:
  0 — reconciliation completed (any number of crashes detected)
  1 — fatal error (e.g. cannot open the DB)

CLI:
  python liveness.py                # one-shot reconciliation
  python liveness.py --json         # also print a summary as JSON on stdout
  python liveness.py --install      # install the systemd user timer (60s interval)
  python liveness.py --uninstall    # remove the systemd user timer
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import spawner
import store

TIMER_UNIT = "taskpilot-liveness.timer"
SERVICE_UNIT = "taskpilot-liveness.service"
INTERVAL_SECONDS = 60


def _state_dir(task_id: str) -> Path:
    d = Path.home() / ".taskpilot" / task_id / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_event(task_id: str, key: str, record: dict) -> None:
    sd = _state_dir(task_id)
    with (sd / "events.jsonl").open("a") as f:
        f.write(json.dumps({"key": key, **record}) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile_once(db_path: str | None = None) -> dict:
    """Single reconciliation pass. Returns counts {checked, alive, crashed}."""
    conn = store.get_db(db_path)
    try:
        running = store.list_tasks(conn, status="running")
        alive = 0
        crashed = []
        for task in running:
            tid = task["task_id"]
            if spawner.is_tmux_alive(tid):
                store.mark_seen(conn, tid)
                alive += 1
            else:
                reason = "tmux session not found"
                store.mark_crashed(conn, tid, reason)
                _append_event(tid, "liveness_crash", {
                    "received_at": _now_iso(),
                    "reason": reason,
                    "previous_status": "running",
                })
                crashed.append(tid)
        return {"checked": len(running), "alive": alive, "crashed": crashed}
    finally:
        conn.close()


def _systemd_user_dir() -> Path:
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


def install_timer() -> None:
    """Install a systemd user timer that runs the reconciler every 60s."""
    script = Path(__file__).resolve()
    python = sys.executable or "/usr/bin/python3"
    unit_dir = _systemd_user_dir()

    service_body = f"""[Unit]
Description=taskpilot liveness reconciler (one-shot)
After=default.target

[Service]
Type=oneshot
ExecStart={python} {script}
"""

    timer_body = f"""[Unit]
Description=taskpilot liveness reconciler (every {INTERVAL_SECONDS}s)

[Timer]
OnBootSec=30
OnUnitActiveSec={INTERVAL_SECONDS}
AccuracySec=5
Unit={SERVICE_UNIT}

[Install]
WantedBy=timers.target
"""

    (unit_dir / SERVICE_UNIT).write_text(service_body)
    (unit_dir / TIMER_UNIT).write_text(timer_body)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", TIMER_UNIT], check=True)
    print(f"installed {TIMER_UNIT} (every {INTERVAL_SECONDS}s)")


def uninstall_timer() -> None:
    """Disable and remove the systemd user timer."""
    subprocess.run(["systemctl", "--user", "disable", "--now", TIMER_UNIT],
                   check=False)
    unit_dir = _systemd_user_dir()
    for unit in (TIMER_UNIT, SERVICE_UNIT):
        path = unit_dir / unit
        if path.exists():
            path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"removed {TIMER_UNIT}")


def main() -> int:
    args = sys.argv[1:]
    if "--install" in args:
        install_timer()
        return 0
    if "--uninstall" in args:
        uninstall_timer()
        return 0

    summary = reconcile_once()
    if "--json" in args:
        print(json.dumps(summary))
    else:
        print(f"checked={summary['checked']} alive={summary['alive']} "
              f"crashed={len(summary['crashed'])}", end="")
        if summary["crashed"]:
            print(f" → {','.join(summary['crashed'])}")
        else:
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
