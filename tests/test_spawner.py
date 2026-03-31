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

        assert "## Memory" in md
        assert "store_memory" in md

        assert "## Scheduling" in md
        assert "schedule_task" in md

        assert "## Human Approval" in md
        assert "request_approval" in md

    def test_memory_only_when_declared(self):
        md = spawner._build_claude_md("Test", "desc", {"capabilities": ["scheduling"]})
        assert "## Memory" not in md
        assert "## Scheduling" in md

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
    def test_empty_capabilities(self):
        assert spawner.resolve_capabilities([]) == []

    def test_missing_nov_hub(self, tmp_path):
        """Should return empty list if nov-dependency-resolver doesn't exist."""
        with patch.object(spawner, 'PLUGIN_ROOT', tmp_path):
            # tmp_path.parent / "nov-dependency-resolver" won't exist
            result = spawner.resolve_capabilities(["memory"])
            assert result == []
