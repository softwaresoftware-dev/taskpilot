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
