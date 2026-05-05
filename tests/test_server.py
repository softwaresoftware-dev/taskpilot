"""Tests for the MCP server — create_task with operating brief."""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import store
import spawner
import server

# Save reference before patching
_real_get_db = store.get_db


@pytest.fixture
def db_path(tmp_path):
    """Return a path to a fresh test database."""
    return str(tmp_path / "test.db")


class TestCreateTaskIntegration:
    """Test create_task with operating brief end-to-end (minus tmux)."""

    def test_create_with_brief(self, db_path, tmp_path):
        brief = {
            "objectives": ["goal1"],
            "capabilities": ["memory"],
        }
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
            patch("server.spawner.resolve_capabilities", return_value=[]),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')

            result = server.create_task(
                name="test brief task",
                description="Test with brief",
                operating_brief=brief,
            )

            assert "error" not in result
            assert result["task_id"] == "test-brief-task"

            # Verify brief stored in DB via fresh connection
            conn = _real_get_db(db_path)
            task = store.get_task(conn, "test-brief-task")
            conn.close()
            stored = json.loads(task["operating_brief"])
            assert stored["objectives"] == ["goal1"]

            # Verify CLAUDE.md was generated dynamically
            md = (tmp_path / "test-brief-task" / "CLAUDE.md").read_text()
            assert "- goal1" in md
            assert "## Memory" in md

    def test_create_backward_compat(self, db_path, tmp_path):
        """Old-style create_task with no brief still works."""
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')

            result = server.create_task(
                name="simple task",
                description="Just do it",
            )

            assert "error" not in result
            md = (tmp_path / "simple-task" / "CLAUDE.md").read_text()
            assert "## Mission\nJust do it" in md
            assert "## Objectives" not in md

    def test_capability_resolution_merges_plugins(self, db_path, tmp_path):
        """Capabilities resolved by nov-dependency-resolver are merged with explicit plugins."""
        brief = {"capabilities": ["memory", "notification"]}
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
            patch("server.spawner.resolve_capabilities", return_value=["/resolved/memory-file"]),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')

            result = server.create_task(
                name="cap test",
                description="Test caps",
                plugins=["/explicit/plugin"],
                operating_brief=brief,
            )

            conn = _real_get_db(db_path)
            plugins = json.loads(store.get_task(conn, "cap-test")["plugins"])
            conn.close()
            assert "/explicit/plugin" in plugins
            assert "/resolved/memory-file" in plugins

    def test_duplicate_prevention(self, db_path, tmp_path):
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')

            server.create_task(name="dup task", description="first")
            result = server.create_task(name="dup task", description="second")
            assert "error" in result

    def test_create_with_host(self, db_path, tmp_path):
        """host parameter is stored on the task row for spawn_task to consult."""
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')

            result = server.create_task(
                name="phone task",
                description="send a text",
                host="pixel-7-pro",
            )
            assert "error" not in result, result
            assert result.get("host") == "pixel-7-pro"

            conn = _real_get_db(db_path)
            stored = store.get_task(conn, "phone-task")
            conn.close()
            assert stored["host"] == "pixel-7-pro"

    def test_create_rejects_remote_service_kind(self, db_path, tmp_path):
        """Remote /spawn doesn't currently install systemd; reject up front."""
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')

            result = server.create_task(
                name="phone svc",
                description="x",
                host="pixel-7-pro",
                kind="service",
            )
            assert "error" in result
            assert "service" in result["error"].lower()


class TestSpawnTaskHostDispatch:
    """spawn_task branches on task.host: local tmux vs forward to peer /spawn."""

    def test_spawn_remote_host_calls_spawn_remote(self, db_path, tmp_path):
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
            server.create_task(name="phone task", description="hi", host="pixel-7-pro")

            with (
                patch.object(spawner, "is_self_host", return_value=False),
                patch.object(spawner, "spawn_remote", return_value={
                    "spawned": True,
                    "session_id": "remote-uuid",
                    "tmux_session": "spawn-phone-task.taskpilot",
                }) as mock_remote,
            ):
                result = server.spawn_task("phone-task")

            assert mock_remote.called
            assert result["status"] == "running"
            assert result["host"] == "pixel-7-pro"
            assert result["remote_session_id"] == "remote-uuid"

    def test_spawn_remote_failure_surfaces_error(self, db_path, tmp_path):
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
            server.create_task(name="phone task", description="hi", host="pixel-7-pro")

            with (
                patch.object(spawner, "is_self_host", return_value=False),
                patch.object(spawner, "spawn_remote", return_value={
                    "spawned": False,
                    "error": "peer pixel-7-pro unreachable: connection refused",
                }),
            ):
                result = server.spawn_task("phone-task")

            assert "error" in result
            assert "pixel-7-pro" in result["error"]

    def test_spawn_self_host_falls_through_to_local(self, db_path, tmp_path):
        """host=local-yocal where local-yocal is self → existing local tmux path."""
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
            server.create_task(name="local task", description="hi", host="local-yocal")

            with (
                patch.object(spawner, "is_self_host", return_value=True),
                patch.object(spawner, "spawn_remote") as mock_remote,
                patch.object(spawner, "spawn_tmux", return_value=True) as mock_local,
                patch.object(spawner, "send_initial_prompt"),
                patch.object(spawner, "channel_healthy", return_value=True),
                patch.object(spawner, "tmux_session_name", return_value="taskpilot-local-task"),
            ):
                result = server.spawn_task("local-task")

            mock_remote.assert_not_called()
            mock_local.assert_called_once()
            assert result["status"] == "running"


class TestDaemonDispatch:
    """spawn/kill/message route through the supervisor daemon when it's up."""

    def test_spawn_uses_daemon_when_reachable(self, db_path, tmp_path):
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
            server.create_task(name="d task", description="hi")

            daemon_response = {
                "status": "running",
                "task_id": "d-task",
                "kind": "task",
                "tmux_session": "d-task",
                "channel_healthy": True,
            }
            with (
                patch("server._daemon_call", return_value=daemon_response) as mock_dc,
                patch.object(spawner, "spawn_tmux") as mock_local,
            ):
                result = server.spawn_task("d-task")

            mock_dc.assert_called_once_with("POST", "/tasks/d-task/spawn")
            mock_local.assert_not_called()  # daemon won — no fallback
            assert result == daemon_response

    def test_spawn_falls_back_to_direct_when_daemon_down(self, db_path, tmp_path):
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
            server.create_task(name="d task", description="hi")

            with (
                patch("server._daemon_call", return_value=None) as mock_dc,
                patch.object(spawner, "spawn_tmux", return_value=True) as mock_local,
                patch.object(spawner, "send_initial_prompt"),
                patch.object(spawner, "tmux_session_name", return_value="d-task"),
            ):
                result = server.spawn_task("d-task")

            mock_dc.assert_called_once()
            mock_local.assert_called_once()  # fell back
            assert result["status"] == "running"

    def test_kill_uses_daemon_when_reachable(self, db_path, tmp_path):
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
            server.create_task(name="d task", description="hi")

            daemon_response = {"task_id": "d-task", "status": "killed", "tmux_killed": True}
            with (
                patch("server._daemon_call", return_value=daemon_response) as mock_dc,
                patch.object(spawner, "kill_tmux") as mock_local,
            ):
                result = server.kill_task("d-task")

            mock_dc.assert_called_once_with("POST", "/tasks/d-task/kill")
            mock_local.assert_not_called()
            assert result == daemon_response

    def test_message_uses_daemon_when_reachable(self, db_path, tmp_path):
        with (
            patch("server.store.get_db", side_effect=lambda: _real_get_db(db_path)),
            patch.object(spawner, "TASKPILOT_DIR", tmp_path),
            patch.object(spawner, "CLAUDE_JSON", tmp_path / ".claude.json"),
        ):
            (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
            server.create_task(name="d task", description="hi")

            daemon_response = {"delivered": True, "response": "ok (chat_id: 3)"}
            with patch("server._daemon_call", return_value=daemon_response) as mock_dc:
                result = server.send_message("d-task", "hello")

            mock_dc.assert_called_once_with(
                "POST", "/tasks/d-task/message",
                json_body={"text": "hello", "from_session": "taskpilot-mcp"},
            )
            assert result == daemon_response
