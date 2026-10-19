"""Sandbox mode: the API carries the spec, launch files stay out of the
agent's reach, the flags take away code, web and other sessions, and the HTTP
tool only lets allowlisted routes through."""

import json
import os
import pathlib
import stat
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sandbox_http  # noqa: E402
import spawner  # noqa: E402
from test_daemon_api import _put, client, rt  # noqa: E402,F401

SPEC = {"tools": ["Read", "Grep"],
        "http": {"base": "http://127.0.0.1:8745", "allow": [["POST", "/api/deals/7/extractions/3"]],
                 "headers": {"Authorization": "Bearer s3cret"}}}


def test_sandbox_is_stored_passed_to_the_spawner_and_never_echoed(rt):
    r = _put(sandbox=SPEC)
    assert r.status_code == 200 and r.json()["sandbox"]["http"]["headers"] == {"Authorization": "…"}
    assert client.post("/tasks/job-1/start").status_code == 200
    assert rt.spawned["sandbox"]["http"]["headers"]["Authorization"] == "Bearer s3cret"
    shown = client.get("/tasks/job-1").json()["sandbox"]
    assert "s3cret" not in json.dumps(shown) and shown["tools"] == ["Read", "Grep"]
    assert "s3cret" not in json.dumps(client.get("/tasks").json())


def test_code_and_web_tools_are_refused(rt):
    for tools in (["Bash"], ["Read", "WebFetch"], ["PowerShell"]):
        assert _put(sandbox={"tools": tools}).status_code == 422
    assert _put(sandbox={"http": {"base": "file:///etc", "allow": []}}).status_code == 422


def test_unsandboxed_tasks_launch_as_before(rt):
    _put()
    client.post("/tasks/job-1/start")
    assert rt.spawned["sandbox"] is None


def test_launch_files_are_private_and_outside_the_task_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(spawner, "SANDBOX_DIR", tmp_path / ".sandbox")
    env, flags = spawner.sandbox_launch("job-9", SPEC)
    d = spawner.sandbox_dir("job-9")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / "http.json").st_mode) == 0o600
    assert json.loads((d / "http.json").read_text())["headers"]["Authorization"] == "Bearer s3cret"
    assert spawner.task_dir("job-9") not in d.parents and d not in spawner.task_dir("job-9").parents
    assert "s3cret" not in flags and "s3cret" not in env
    mcp = json.loads((d / "mcp.json").read_text())["mcpServers"]
    assert list(mcp) == ["sandbox"]
    assert json.loads((d / "settings.json").read_text()) == {"enabledPlugins": {spawner.CHANNEL_PLUGIN: True}}


def test_launch_flags_take_away_code_web_and_other_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(spawner, "SANDBOX_DIR", tmp_path / ".sandbox")
    env, flags = spawner.sandbox_launch("job-9", SPEC)
    assert "--dangerously-skip-permissions" not in flags
    for f in ("--restricted", "--tools Read,Grep", "--permission-mode dontAsk", "mcp__sandbox__http",
              "--disallowedTools mcp__plugin_session-bridge_session-bridge__message "
              "mcp__plugin_session-bridge_session-bridge__sessions"):
        assert f in flags
    assert "ENABLE_CLAUDEAI_MCP_SERVERS=false" in env
    _, bare = spawner.sandbox_launch("job-8", {})
    assert "--tools Read,Glob,Grep" in bare and "mcp__sandbox__http" not in bare
    with pytest.raises(ValueError):
        spawner.sandbox_launch("job-7", {"tools": ["Bash"]})


@pytest.mark.parametrize("method,path,ok", [
    ("POST", "/api/deals/7/extractions/3", True),
    ("post", "/api/deals/7/extractions/3", True),
    ("POST", "/api/deals/7/extractions/3/extra", True),     # under the prefix
    ("POST", "/api/deals/7/extractions/30", False),         # not a path segment of it
    ("GET", "/api/deals/7/extractions/3", False),           # wrong method
    ("POST", "/api/deals/7/extractions/3/../../../notify", False),
    ("POST", "/api/deals/7/extractions/3/%2e%2e/%2e%2e/notify", False),
    ("POST", "http://evil.example/api/deals/7/extractions/3", False),
    ("POST", "//evil.example/api/deals/7/extractions/3", False),
    ("POST", "api/deals/7/extractions/3", False),
])
def test_http_tool_allowlist(method, path, ok):
    cfg = {"base": "http://127.0.0.1:1", "allow": [("POST", "/api/deals/7/extractions/3")]}
    assert (sandbox_http.allowed(cfg, method, path) is None) is ok


def test_refused_calls_never_leave_the_box(monkeypatch):
    sent = []
    monkeypatch.setattr(sandbox_http.urllib.request, "build_opener", lambda *a: sent.append(a))
    cfg = {"base": "http://127.0.0.1:1", "allow": [("POST", "/api/x")], "headers": {}}
    assert sandbox_http.call(cfg, "POST", "/api/notify", {"a": 1}).startswith("REFUSED")
    assert sent == []
