"""Tests for pane.log capture in spawner.py — pipe-pane install, separator
writes, size cap.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import spawner
from spawner import (
    PANE_LOG_MAX_BYTES_DEFAULT,
    PANE_LOG_MIN_BYTES,
    _install_pipe_pane,
    _pane_log_max_bytes,
    _setup_pane_log_capture,
    _truncate_if_oversize,
    _write_invocation_separator,
    pane_log_path,
)


@pytest.fixture
def isolated_taskpilot_dir(tmp_path, monkeypatch):
    """Redirect taskpilot state dir into a temp tree."""
    monkeypatch.setattr(spawner, "TASKPILOT_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def fake_store(monkeypatch):
    """Stub out store.get_db / get_task / increment_invocation."""
    fake = MagicMock()
    fake_conn = MagicMock()
    fake.get_db.return_value = fake_conn
    fake.get_task.return_value = {"task_id": "t1", "invocation_count": 0}
    monkeypatch.setitem(sys.modules, "store", fake)
    return fake


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# --- _install_pipe_pane -------------------------------------------------------

def test_install_pipe_pane_uses_stdbuf_when_available(tmp_path):
    log = tmp_path / "pane.log"
    with patch("spawner.shutil.which", return_value="/usr/bin/stdbuf"), \
         patch("spawner.subprocess.run", return_value=_proc(0)) as run:
        ok = _install_pipe_pane("session-x", log)
    assert ok is True
    args = run.call_args[0][0]
    assert args[:3] == ["tmux", "pipe-pane", "-t"]
    assert args[3] == "session-x:0.0"
    # Final shell command is the last arg
    assert args[4].startswith("stdbuf -o0 cat >> ")
    # Path is shell-quoted
    assert str(log) in args[4]


def test_install_pipe_pane_falls_back_when_stdbuf_missing(tmp_path):
    log = tmp_path / "pane.log"
    with patch("spawner.shutil.which", return_value=None), \
         patch("spawner.subprocess.run", return_value=_proc(0)) as run:
        ok = _install_pipe_pane("session-x", log)
    assert ok is True
    assert run.call_args[0][0][4].startswith("cat >> ")


def test_install_pipe_pane_returns_false_on_nonzero(tmp_path):
    log = tmp_path / "pane.log"
    with patch("spawner.shutil.which", return_value="/usr/bin/stdbuf"), \
         patch("spawner.subprocess.run", return_value=_proc(1, stderr="boom")):
        assert _install_pipe_pane("session-x", log) is False


def test_install_pipe_pane_returns_false_on_exception(tmp_path):
    log = tmp_path / "pane.log"
    with patch("spawner.shutil.which", return_value="/usr/bin/stdbuf"), \
         patch("spawner.subprocess.run", side_effect=OSError("kernel hates us")):
        assert _install_pipe_pane("session-x", log) is False


def test_install_pipe_pane_quotes_paths_with_spaces(tmp_path):
    log = tmp_path / "with space" / "pane.log"
    log.parent.mkdir()
    with patch("spawner.shutil.which", return_value="/usr/bin/stdbuf"), \
         patch("spawner.subprocess.run", return_value=_proc(0)) as run:
        _install_pipe_pane("session-x", log)
    cmd = run.call_args[0][0][4]
    # Path must be safely quoted such that the shell sees one arg
    assert "'" in cmd or '"' in cmd  # shlex.quote produces single-quote wrapping


# --- _write_invocation_separator ---------------------------------------------

def test_separator_first_invocation(tmp_path, fake_store):
    log = tmp_path / "pane.log"
    fake_store.get_task.return_value = {"invocation_count": 0}
    _write_invocation_separator(log, "task-1")
    content = log.read_text()
    assert "invocation 1" in content
    assert "reason=start" in content


def test_separator_respawn_invocation(tmp_path, fake_store):
    log = tmp_path / "pane.log"
    fake_store.get_task.return_value = {"invocation_count": 2}
    _write_invocation_separator(log, "task-1")
    content = log.read_text()
    assert "invocation 3" in content
    assert "reason=respawn" in content


def test_separator_creates_parent_dir(tmp_path, fake_store):
    log = tmp_path / "deeper" / "pane.log"
    _write_invocation_separator(log, "task-1")
    assert log.exists()


def test_separator_appends_to_existing(tmp_path, fake_store):
    log = tmp_path / "pane.log"
    log.write_text("prior content\n")
    _write_invocation_separator(log, "task-1")
    content = log.read_text()
    assert content.startswith("prior content\n")
    assert "invocation 1" in content


def test_separator_file_mode_is_0600(tmp_path, fake_store):
    log = tmp_path / "pane.log"
    _write_invocation_separator(log, "task-1")
    if os.name == "posix":
        assert (log.stat().st_mode & 0o777) == 0o600


# --- _truncate_if_oversize ---------------------------------------------------

def test_truncate_no_op_when_absent(tmp_path):
    log = tmp_path / "missing.log"
    _truncate_if_oversize(log)  # no exception
    assert not log.exists()


def test_truncate_no_op_under_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKPILOT_PANE_LOG_MAX_BYTES", "8192")
    log = tmp_path / "pane.log"
    log.write_bytes(b"x" * 1000)
    _truncate_if_oversize(log)
    assert log.stat().st_size == 1000


def test_truncate_at_cap_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKPILOT_PANE_LOG_MAX_BYTES", "8192")
    log = tmp_path / "pane.log"
    log.write_bytes(b"x" * 8192)
    _truncate_if_oversize(log)
    assert log.stat().st_size == 8192


def test_truncate_over_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKPILOT_PANE_LOG_MAX_BYTES", "8192")
    log = tmp_path / "pane.log"
    log.write_bytes(b"a" * 5000 + b"b" * 5000)  # 10000 bytes > 8192 cap
    _truncate_if_oversize(log)
    new_size = log.stat().st_size
    # cap/2 = 4096 bytes kept + marker; new size should be ~4096+marker, ≤ cap
    assert new_size <= 8192
    content = log.read_bytes()
    # Latest content (b's) preserved at end
    assert content.endswith(b"b" * 100)
    # Truncation marker present
    assert b"truncated" in content


def test_pane_log_max_bytes_env_override(monkeypatch):
    monkeypatch.setenv("TASKPILOT_PANE_LOG_MAX_BYTES", "8192")
    assert _pane_log_max_bytes() == 8192


def test_pane_log_max_bytes_clamps_to_min(monkeypatch):
    monkeypatch.setenv("TASKPILOT_PANE_LOG_MAX_BYTES", "100")
    assert _pane_log_max_bytes() == PANE_LOG_MIN_BYTES


def test_pane_log_max_bytes_default(monkeypatch):
    monkeypatch.delenv("TASKPILOT_PANE_LOG_MAX_BYTES", raising=False)
    assert _pane_log_max_bytes() == PANE_LOG_MAX_BYTES_DEFAULT


def test_pane_log_max_bytes_garbage_returns_default(monkeypatch):
    monkeypatch.setenv("TASKPILOT_PANE_LOG_MAX_BYTES", "abc")
    assert _pane_log_max_bytes() == PANE_LOG_MAX_BYTES_DEFAULT


# --- _setup_pane_log_capture -------------------------------------------------

def test_setup_writes_separator_even_when_pipe_fails(isolated_taskpilot_dir, fake_store):
    task_id = "task-1"
    (isolated_taskpilot_dir / task_id).mkdir()
    with patch("spawner._install_pipe_pane", return_value=False):
        _setup_pane_log_capture(task_id, "session-x")
    log = pane_log_path(task_id)
    assert log.exists()
    assert "invocation 1" in log.read_text()


def test_setup_pane_log_path_helpers(isolated_taskpilot_dir):
    assert pane_log_path("foo") == isolated_taskpilot_dir / "foo" / "pane.log"
