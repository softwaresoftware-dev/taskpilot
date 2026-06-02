"""Tests for channel-reference validation and wait-for-channel polling."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import spawner


@pytest.fixture()
def fake_registries(tmp_path, monkeypatch):
    """Point CLAUDE_JSON and INSTALLED_PLUGINS_PATH at empty tmp files.

    Tests opt in to specific contents by writing into these paths.
    """
    claude_json = tmp_path / ".claude.json"
    installed = tmp_path / "installed_plugins.json"
    claude_json.write_text(json.dumps({"mcpServers": {}}))
    installed.write_text(json.dumps({"version": 2, "plugins": {}}))
    monkeypatch.setattr(spawner, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(spawner, "INSTALLED_PLUGINS_PATH", installed)
    return {"claude_json": claude_json, "installed": installed}


class TestValidateChannelsServerForm:
    def test_resolves_when_mcp_configured(self, fake_registries):
        fake_registries["claude_json"].write_text(json.dumps({
            "mcpServers": {"my-bridge": {"command": "node", "args": ["x.js"]}}
        }))
        # Should not raise.
        spawner.validate_channels(["server:my-bridge"])

    def test_raises_when_mcp_missing(self, fake_registries):
        with pytest.raises(spawner.ChannelResolutionError) as exc:
            spawner.validate_channels(["server:nope"])
        assert "server:nope" in str(exc.value)
        assert "nope" in str(exc.value)
        assert "claude mcp add" in str(exc.value)

    def test_distinguishes_servers_with_similar_names(self, fake_registries):
        fake_registries["claude_json"].write_text(json.dumps({
            "mcpServers": {"foo-bar": {}}
        }))
        # Configured server: foo-bar. Requesting server: foo (substring) must fail.
        with pytest.raises(spawner.ChannelResolutionError):
            spawner.validate_channels(["server:foo"])
        # Exact match passes.
        spawner.validate_channels(["server:foo-bar"])


class TestValidateChannelsPluginForm:
    def test_resolves_when_plugin_installed(self, fake_registries):
        fake_registries["installed"].write_text(json.dumps({
            "version": 2,
            "plugins": {
                "session-bridge@softwaresoftware-plugins": [{"installPath": "/x"}],
            },
        }))
        spawner.validate_channels(["plugin:session-bridge@softwaresoftware-plugins"])

    def test_raises_when_plugin_missing(self, fake_registries):
        with pytest.raises(spawner.ChannelResolutionError) as exc:
            spawner.validate_channels(["plugin:not-there@softwaresoftware-plugins"])
        msg = str(exc.value)
        assert "plugin:not-there@softwaresoftware-plugins" in msg
        assert "not-there" in msg
        assert "/softwaresoftware:install not-there" in msg

    def test_marketplace_qualifier_does_not_affect_match(self, fake_registries):
        # installed_plugins.json keys split on @ — the marketplace tag in the
        # channel ref is a user-facing assertion, not part of the install lookup.
        fake_registries["installed"].write_text(json.dumps({
            "version": 2,
            "plugins": {"foo@some-marketplace": [{"installPath": "/x"}]},
        }))
        # Plugin "foo" is installed (regardless of marketplace it came from).
        spawner.validate_channels(["plugin:foo@any-marketplace"])


class TestValidateChannelsMisc:
    def test_empty_list_passes(self, fake_registries):
        spawner.validate_channels([])
        spawner.validate_channels(None)  # type: ignore — defensive

    def test_unknown_kind_passes_through(self, fake_registries):
        # Forward-compat: future channel forms shouldn't be blocked here.
        spawner.validate_channels(["future-kind:whatever"])

    def test_mix_of_valid_and_invalid_raises_on_first_invalid(self, fake_registries):
        fake_registries["claude_json"].write_text(json.dumps({
            "mcpServers": {"good": {}}
        }))
        with pytest.raises(spawner.ChannelResolutionError) as exc:
            spawner.validate_channels(["server:good", "server:bad"])
        assert "server:bad" in str(exc.value)


class TestWaitForChannel:
    def test_returns_true_on_immediate_success(self):
        with patch.object(spawner, "channel_healthy", return_value=True), \
             patch.object(spawner.time, "sleep") as mock_sleep:
            assert spawner.wait_for_channel("my-task", timeout=5) is True
            mock_sleep.assert_not_called()  # never sleeps if first poll succeeds

    def test_returns_true_after_a_few_polls(self):
        # First two polls False, third True.
        results = iter([False, False, True])
        with patch.object(spawner, "channel_healthy", side_effect=lambda _: next(results)), \
             patch.object(spawner.time, "sleep") as mock_sleep:
            assert spawner.wait_for_channel("my-task", timeout=5) is True
            assert mock_sleep.call_count == 2  # slept after each False

    def test_returns_false_on_timeout(self):
        # All polls return False. Should return False, not True.
        with patch.object(spawner, "channel_healthy", return_value=False), \
             patch.object(spawner.time, "sleep"):
            assert spawner.wait_for_channel("my-task", timeout=3) is False

    def test_timeout_zero_returns_false_without_polling(self):
        with patch.object(spawner, "channel_healthy") as mock_check, \
             patch.object(spawner.time, "sleep"):
            assert spawner.wait_for_channel("my-task", timeout=0) is False
            mock_check.assert_not_called()


class TestSpawnTmuxChannelPatience:
    """A channel that doesn't register within the wait window is NOT a spawn
    failure: registration is eventually-consistent via channel.mjs's heartbeat,
    which re-registers once the bridge is reachable. spawn_tmux must return True
    (spawned) so the reconciler doesn't mark a briefly-bridgeless service
    'crashed'. Only an actual tmux launch failure returns False.
    """

    def _patch_internals(self, monkeypatch):
        monkeypatch.setattr(spawner, "write_hook_settings", lambda tid: "/tmp/hs.json")
        monkeypatch.setattr(spawner, "prepare_sandbox", lambda *a, **k: "/tmp/home")
        monkeypatch.setattr(spawner, "validate_channels", lambda chans: None)
        monkeypatch.setattr(spawner, "_setup_pane_log_capture", lambda *a, **k: None)
        monkeypatch.setattr(spawner.time, "sleep", lambda *a, **k: None)

    def test_returns_true_when_channel_pending(self, monkeypatch):
        # tmux launches fine, but the channel never registers in the window.
        self._patch_internals(monkeypatch)
        monkeypatch.setattr(spawner, "wait_for_channel", lambda *a, **k: False)
        ok = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        monkeypatch.setattr(spawner.subprocess, "run", lambda *a, **k: ok)
        assert spawner.spawn_tmux("svc-1", [], kind="service") is True

    def test_returns_false_when_tmux_launch_fails(self, monkeypatch):
        # Even with a healthy channel, a tmux launch failure is a real failure.
        self._patch_internals(monkeypatch)
        monkeypatch.setattr(spawner, "wait_for_channel", lambda *a, **k: True)
        fail = type("P", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        monkeypatch.setattr(spawner.subprocess, "run", lambda *a, **k: fail)
        assert spawner.spawn_tmux("svc-1", [], kind="service") is False
