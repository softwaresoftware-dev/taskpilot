"""Classify an agent's final assistant message into an action bucket.

Used by hooks/on-stop.py to act in real time when the agent finishes a turn.

Three buckets:
  resolved    — the agent has met the task's success criteria, or declared it done.
  question    — the agent ended with a direct question to the human.
  uneventful  — neither; agent stopped mid-flow or paused without asking.

The classifier shells out to `claude -p` with the task's success_criteria
(loaded from ~/.taskpilot/<task_id>/brief.json) and the agent's final message,
asks Haiku for a one-word verdict, and parses it back.

Two env-vars are stripped from the judge subprocess:
  ANTHROPIC_API_KEY — a stale key in the spawning shell shadows OAuth keychain
                      auth and breaks the judge with "invalid x-api-key".
  TASKPILOT_TASK_ID — inheriting it would make the judge fire its own Stop
                      hook on completion and recurse into another classifier
                      run.

Failure mode: any subprocess error, timeout, or unparseable output → uneventful.
False-negatives leave the agent running (cheap), false-positives kill it
prematurely (expensive). When in doubt, don't kill.
"""

import json
import os
import subprocess
from pathlib import Path

_VALID_BUCKETS = ("resolved", "question", "uneventful")
# Spawned agents run with the user's real $HOME, so ~/.taskpilot/ is the same
# directory the daemon reads.
_TASKPILOT_DIR = Path.home() / ".taskpilot"
_JUDGE_TIMEOUT_S = 60
_JUDGE_MODEL = "haiku"
_STRIPPED_ENV_VARS = ("ANTHROPIC_API_KEY", "TASKPILOT_TASK_ID")


def _load_brief(tid: str) -> dict:
    """Load brief.json for a task. Returns {} if missing/unreadable."""
    path = _TASKPILOT_DIR / tid / "brief.json"
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _build_prompt(message: str, brief: dict) -> str:
    """Build the judge prompt from the agent's final message + brief."""
    objectives = brief.get("objectives") or []
    success_criteria = brief.get("success_criteria") or []
    boundaries = brief.get("boundaries") or []

    sections = []
    if objectives:
        sections.append("Objectives:\n" + "\n".join(f"- {o}" for o in objectives))
    if success_criteria:
        sections.append("Success criteria:\n" + "\n".join(f"- {s}" for s in success_criteria))
    if boundaries:
        sections.append("Boundaries:\n" + "\n".join(f"- {b}" for b in boundaries))
    brief_section = "\n\n".join(sections) if sections else "(no brief available)"

    return (
        "You are judging whether an autonomous agent has finished its task. "
        "Read the brief and the agent's final message, then respond with EXACTLY "
        "one word from this set: resolved, question, uneventful.\n\n"
        "- resolved: the agent met the success criteria or declared the task done.\n"
        "- question: the agent ended by asking the human for input.\n"
        "- uneventful: neither — the agent stopped mid-flow without finishing.\n\n"
        "When in doubt, prefer uneventful (false-positive resolved kills the agent).\n\n"
        f"=== Brief ===\n{brief_section}\n\n"
        f"=== Final message ===\n{message}\n\n"
        "Respond with one word only."
    )


def _judge_env() -> dict:
    """Build the env for the judge subprocess: strip stale API key + task id."""
    return {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS}


def _run_judge(prompt: str) -> str | None:
    """Invoke `claude -p` and return its stripped stdout, or None on failure.

    Prompt is fed via stdin (not as an argv positional) so we sidestep the
    `--tools ""` variadic-flag trap where claude swallows the next argv as a
    tool name.
    """
    try:
        result = subprocess.run(
            ["claude", "--print", "--model", _JUDGE_MODEL],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_JUDGE_TIMEOUT_S,
            env=_judge_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _parse_verdict(raw: str | None) -> str:
    """Pick the bucket from the judge's raw output. Default uneventful."""
    if not raw:
        return "uneventful"
    lowered = raw.lower()
    for bucket in _VALID_BUCKETS:
        if bucket in lowered:
            return bucket
    return "uneventful"


def classify(message: str, task_id: str | None = None) -> str:
    """Return one of: 'resolved', 'question', 'uneventful'.

    `task_id` is used to load the operating brief; if absent, the judge sees
    only the message and has to fall back on its own priors.
    """
    if not message:
        return "uneventful"

    brief = _load_brief(task_id) if task_id else {}
    prompt = _build_prompt(message, brief)
    raw = _run_judge(prompt)
    return _parse_verdict(raw)
