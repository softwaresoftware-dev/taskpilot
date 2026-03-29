#!/usr/bin/env bash
# taskpilot-recover.sh — Respawn tasks that were running before reboot/crash.
# Reads taskpilot.db for tasks with status="running" that have no tmux session.
# Run manually, from /taskpilot:status, or via systemd user service on login.

set -e

TASKPILOT_DIR="$HOME/.taskpilot"
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$TASKPILOT_DIR/taskpilot.db"

if [ ! -f "$DB" ]; then
  echo "No taskpilot database found at $DB"
  exit 0
fi

# Find tasks that are "running" in DB but have no tmux session
ORPHANED=$(python3 -c "
import sqlite3, subprocess, json

conn = sqlite3.connect('$DB')
conn.row_factory = sqlite3.Row
tasks = conn.execute(\"SELECT task_id, port FROM tasks WHERE status = 'running'\").fetchall()
conn.close()

orphaned = []
for t in tasks:
    tid = t['task_id']
    session = f'taskpilot-{tid}'
    result = subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True)
    if result.returncode != 0:
        orphaned.append({'task_id': tid, 'port': t['port']})

print(json.dumps(orphaned))
" 2>/dev/null)

COUNT=$(echo "$ORPHANED" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  echo "No orphaned tasks to recover."
  exit 0
fi

echo "Found $COUNT orphaned task(s). Respawning..."

echo "$ORPHANED" | python3 -c "
import json, sys, subprocess

tasks = json.load(sys.stdin)
for t in tasks:
    tid = t['task_id']
    port = t['port']
    print(f'  Respawning {tid} on port {port}...')
    result = subprocess.run(
        ['python3', '$PLUGIN_DIR/spawner_cli.py', '--name', tid, 'Resume task from crash recovery. Read state.json for previous progress.'],
        capture_output=True, text=True
    )
    print(f'    {result.stdout.strip()}')
" 2>/dev/null

echo "Recovery complete."
