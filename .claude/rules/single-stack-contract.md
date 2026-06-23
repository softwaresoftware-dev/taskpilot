---
description: Per-task HOME + subscription-auth couplings the mindframe single-stack relies on
globs: ["daemon.py", "store.py", "spawner.py"]
---

# Single-stack runtime contract (taskpilot side)

taskpilot is the agent runtime for mindframe's **single-stack** model: ONE
taskpilot daemon serves every workspace by giving each spawned agent a **per-task
`$HOME`** (its workspace partition). Two cross-repo couplings live here:

- **Per-task home:** a task's `home` field — `daemon.py`
  (`TaskDefinition`/`CreateSpawnRequest`/`_upsert`/`_start`), `store.py` (the
  `home` column + migration), `spawner.py` (`export HOME=`). **Consumers pass it:**
  mindframe's dashboard (`home` = the frame's partition) and the dispatcher's
  `spawn_helper` (`home` = the workspace). Don't change the field name or
  semantics without updating those callers.
- **Subscription-only auth:** `spawner.py` unsets `ANTHROPIC_API_KEY` /
  `ANTHROPIC_AUTH_TOKEN` per spawn (opt-out `TASKPILOT_KEEP_API_KEY`). mindframe
  depends on this — a stray key breaks subscription login.

Full replication map + sync checklist live in the mindframe repo:
`plugins/frameworks/mindframe/docs/single-stack-contract.md`.
