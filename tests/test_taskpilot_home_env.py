"""Regression tests for $TASKPILOT_HOME env override.

Sandboxed agents have HOME pointed at ~/.taskpilot/<id>/ — without the
env override, hook scripts compute paths as
`~/.taskpilot/<id>/.taskpilot/<id>/state/` (nested, daemon-invisible).
The spawner exports TASKPILOT_HOME=<real ~/.taskpilot/> so hook scripts
write to the path the daemon reads. These tests pin that contract."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


@pytest.fixture
def real_taskpilot_dir(tmp_path, monkeypatch):
    """Sandboxed HOME at tmp/sandbox-home, real taskpilot dir at tmp/real-tp."""
    sandbox = tmp_path / "sandbox-home"
    sandbox.mkdir()
    real_tp = tmp_path / "real-tp"
    real_tp.mkdir()
    monkeypatch.setenv("HOME", str(sandbox))
    monkeypatch.setenv("USERPROFILE", str(sandbox))
    monkeypatch.setenv("TASKPILOT_HOME", str(real_tp))
    return real_tp, sandbox


def test_record_state_dir_honors_taskpilot_home(real_taskpilot_dir):
    real_tp, sandbox = real_taskpilot_dir
    # Re-import so module-level path resolution picks up the env.
    for mod in ("_record",):
        if mod in sys.modules:
            del sys.modules[mod]
    import _record
    sd = _record.state_dir("test-task-1")
    # Must be under the REAL taskpilot dir, not the sandbox HOME.
    assert str(sd).startswith(str(real_tp))
    assert str(sd).endswith("test-task-1/state") or str(sd).endswith("test-task-1\\state")
    assert sandbox not in sd.parents
    assert sd.is_dir()


def test_record_state_dir_falls_back_to_path_home(tmp_path, monkeypatch):
    """When TASKPILOT_HOME is unset (direct dev invocation), use Path.home()."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("TASKPILOT_HOME", raising=False)
    for mod in ("_record",):
        if mod in sys.modules:
            del sys.modules[mod]
    import _record
    sd = _record.state_dir("dev-task")
    assert str(sd).startswith(str(tmp_path))
    assert str(sd).endswith("dev-task/state") or str(sd).endswith("dev-task\\state")


def test_store_default_db_path_honors_taskpilot_home(real_taskpilot_dir):
    real_tp, _ = real_taskpilot_dir
    sys.path.insert(0, str(HOOKS_DIR.parent))
    for mod in ("store",):
        if mod in sys.modules:
            del sys.modules[mod]
    import store
    assert str(store.DEFAULT_DB_PATH).startswith(str(real_tp))
    assert str(store.DEFAULT_DB_PATH).endswith("taskpilot.db")


def test_actions_taskpilot_dir_honors_env(real_taskpilot_dir):
    real_tp, _ = real_taskpilot_dir
    sys.path.insert(0, str(HOOKS_DIR.parent))
    for mod in ("actions",):
        if mod in sys.modules:
            del sys.modules[mod]
    import actions
    assert str(actions.TASKPILOT_DIR) == str(real_tp)


def test_no_nested_path_under_any_resolution(real_taskpilot_dir):
    """Belt-and-suspenders: the nested ~/.taskpilot/<id>/.taskpilot/ pattern
    must not appear in any of the resolved paths. That was the bug shape."""
    real_tp, sandbox = real_taskpilot_dir
    sys.path.insert(0, str(HOOKS_DIR.parent))
    for mod in ("_record", "store", "actions"):
        if mod in sys.modules:
            del sys.modules[mod]
    import _record, store, actions

    sd = _record.state_dir("any-id")
    paths = [str(sd), str(store.DEFAULT_DB_PATH), str(actions.TASKPILOT_DIR)]
    for p in paths:
        # The buggy form would have ".taskpilot/<id>/.taskpilot/" somewhere.
        assert "/.taskpilot/" not in p.replace(str(real_tp), "REAL").replace(str(sandbox), "SANDBOX"), \
            f"path looks nested: {p}"
