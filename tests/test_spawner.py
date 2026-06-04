"""Tests for the spawner — config writing, CLAUDE.md generation, capability resolution."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import spawner


class TestSlugify:
    def test_basic(self):
        assert spawner.slugify("Sell my lawnmower") == "sell-my-lawnmower"

    def test_special_chars(self):
        assert spawner.slugify("Run gummymine (v2)!") == "run-gummymine-v2"

    def test_truncation(self):
        long_name = "a" * 100
        assert len(spawner.slugify(long_name)) <= 50

    def test_strips_leading_trailing_hyphens(self):
        assert spawner.slugify("--test--") == "test"


class TestBuildClaudeMd:
    def test_minimal_brief(self):
        md = spawner._build_claude_md("Test Task", "Do something", {})
        assert "# Task: Test Task" in md
        assert "## Mission\nDo something" in md
        assert "## Autonomy Rules" in md
        assert "## On Startup" in md
        # Should NOT have optional sections
        assert "## Objectives" not in md
        assert "## Workflows" not in md
        assert "## Boundaries" not in md
        assert "## Memory" not in md

    def test_full_brief(self):
        brief = {
            "objectives": ["find niches", "validate demand"],
            "workflows": ["research", "analyze", "report"],
            "success_criteria": ["5 niches identified"],
            "boundaries": ["don't spend money", "no social posting"],
            "capabilities": ["memory", "scheduling", "human-approval"],
        }
        md = spawner._build_claude_md("Business Agent", "Run gummymine as a business", brief)

        assert "## Objectives" in md
        assert "- find niches" in md
        assert "- validate demand" in md

        assert "## Workflows" in md
        assert "1. research" in md
        assert "2. analyze" in md
        assert "3. report" in md

        assert "## Success Criteria" in md
        assert "- 5 niches identified" in md

        assert "## Boundaries" in md
        assert "- don't spend money" in md

        # Capability sections describe intent — tool names live in the MCP
        # servers' own descriptions, not in CLAUDE.md.
        assert "## Memory" in md
        assert "across sessions" in md
        assert "Use an available skill or tool" in md

        assert "## Scheduling" in md
        assert "recurring workflows" in md

        assert "## Human Approval" in md
        assert "request human approval" in md

        # And critically — no hardcoded tool names that would silently drift
        # if a capability provider changes its API.
        assert "store_memory" not in md
        assert "schedule_task(" not in md  # the MCP tool name with args
        assert "request_approval" not in md

    def test_memory_only_when_declared(self):
        md = spawner._build_claude_md("Test", "desc", {"capabilities": []})
        assert "## Memory" not in md

    def test_capabilities_gate_their_sections(self):
        """Each capability section appears only when declared."""
        md = spawner._build_claude_md("Test", "desc", {"capabilities": []})
        assert "## Memory" not in md
        assert "## Scheduling" not in md
        assert "## Human Approval" not in md

    def test_always_has_core_sections(self):
        md = spawner._build_claude_md("Test", "desc", {})
        assert "## Autonomy Rules" in md
        assert "## How to Escalate to Human" in md
        assert "## State File" in md
        assert "## Channel Communication" in md
        assert "## On Startup" in md


class TestWriteTaskConfig:
    def test_writes_files(self, tmp_path):
        with patch.object(spawner, 'TASKPILOT_DIR', tmp_path):
            td = spawner.write_task_config("test-task", "Test", "Do something", [])
            assert (td / "CLAUDE.md").exists()
            assert (td / "brief.json").exists()

    def test_brief_json_has_operating_brief(self, tmp_path):
        brief = {"objectives": ["goal1"], "capabilities": ["memory"]}
        with patch.object(spawner, 'TASKPILOT_DIR', tmp_path):
            td = spawner.write_task_config("t1", "T1", "desc", [], operating_brief=brief)
            data = json.loads((td / "brief.json").read_text())
            assert data["operating_brief"] == brief
            assert data["task_id"] == "t1"

    def test_brief_json_backward_compat(self, tmp_path):
        with patch.object(spawner, 'TASKPILOT_DIR', tmp_path):
            td = spawner.write_task_config("t2", "T2", "desc", ["/p1"])
            data = json.loads((td / "brief.json").read_text())
            assert data["operating_brief"] == {}
            assert data["plugins"] == ["/p1"]

    def test_claude_md_dynamic_content(self, tmp_path):
        brief = {
            "objectives": ["find 5 niches"],
            "boundaries": ["no spending"],
            "capabilities": ["memory"],
        }
        with patch.object(spawner, 'TASKPILOT_DIR', tmp_path):
            td = spawner.write_task_config("t3", "Niche Finder", "Find niches", [], brief)
            md = (td / "CLAUDE.md").read_text()
            assert "- find 5 niches" in md
            assert "- no spending" in md
            assert "## Memory" in md

    def test_creates_directory(self, tmp_path):
        with patch.object(spawner, 'TASKPILOT_DIR', tmp_path):
            td = spawner.write_task_config("new-dir", "New", "desc", [])
            assert td.is_dir()


class TestResolveCapabilities:
    """resolve_capabilities now hard-delegates to softwaresoftware. All tests
    mock _import_softwaresoftware to control what find_satisfier returns."""

    @pytest.fixture()
    def sw(self, tmp_path):
        """A mocked softwaresoftware (resolver, registry) pair, scriptable per-test."""
        from unittest.mock import MagicMock
        sw_resolver = MagicMock()
        sw_registry = MagicMock()
        with patch.object(spawner, "_import_softwaresoftware",
                          return_value=(sw_resolver, sw_registry)):
            yield sw_resolver, sw_registry, tmp_path

    def test_resolves_installed_provider(self, sw):
        sw_resolver, sw_registry, tmp_path = sw
        sw_resolver.find_satisfier.return_value = {"type": "plugin", "name": "memory-file"}
        sw_registry.get_plugin_install_path.return_value = tmp_path / "memory-file"
        assert spawner.resolve_capabilities(["memory"]) == [str(tmp_path / "memory-file")]
        sw_resolver.find_satisfier.assert_called_once_with("memory")

    def test_dedupes_one_plugin_many_capabilities(self, sw):
        """A plugin satisfying multiple requested capabilities appears once."""
        sw_resolver, sw_registry, tmp_path = sw
        sw_resolver.find_satisfier.return_value = {"type": "plugin", "name": "multi"}
        sw_registry.get_plugin_install_path.return_value = tmp_path / "multi"
        assert spawner.resolve_capabilities(["memory", "notification"]) == [str(tmp_path / "multi")]

    def test_skips_mcp_and_host_satisfiers(self, sw):
        """type=mcp / type=host / type=none don't add a --plugin-dir."""
        sw_resolver, sw_registry, _ = sw
        sw_resolver.find_satisfier.side_effect = [
            {"type": "mcp", "name": "slack-mcp"},
            {"type": "host", "host": "pixel-7-pro", "self": False},
            {"type": "none"},
        ]
        assert spawner.resolve_capabilities(["notification", "send-sms", "unknown"]) == []
        sw_registry.get_plugin_install_path.assert_not_called()

    def test_skips_when_install_path_missing(self, sw):
        """find_satisfier says plugin, but registry returns no path → skip."""
        sw_resolver, sw_registry, _ = sw
        sw_resolver.find_satisfier.return_value = {"type": "plugin", "name": "ghost"}
        sw_registry.get_plugin_install_path.return_value = None
        assert spawner.resolve_capabilities(["memory"]) == []

    def test_empty_capabilities_short_circuits(self):
        """No capabilities → return [] without touching softwaresoftware."""
        # No mock — would explode if _import_softwaresoftware was called.
        assert spawner.resolve_capabilities([]) == []

    def test_raises_when_softwaresoftware_missing(self, tmp_path):
        """Hard-fail when softwaresoftware isn't installed — no fallback."""
        with patch.object(spawner, "INSTALLED_PLUGINS_PATH", tmp_path / "missing.json"):
            with pytest.raises(RuntimeError, match="softwaresoftware"):
                spawner.resolve_capabilities(["memory"])


