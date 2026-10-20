"""Sandbox mode: the API carries the spec, launch files stay out of the
agent's reach, the flags take away code, web and other sessions, and the HTTP
tool only lets allowlisted routes through."""

import json
import os
import pathlib
import shlex
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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


@pytest.mark.parametrize("entry", [
    ["POST", "/*"],                     # allow-all
    ["POST", "api/x"],                  # no leading slash: could never match
    ["HEAD", "/api/x"],                 # not a method the tool speaks
    ["POST", "/api/x?y=1"],             # query string in a pattern
    ["POST", "/api/%2e%2e/x"],          # encoding in a pattern
    ["POST", "/api/x/*/y"],             # wildcard only at the end
])
def test_unusable_allow_entries_are_refused_at_put(rt, entry):
    r = _put(sandbox={"http": {"base": "http://127.0.0.1:1", "allow": [entry]}})
    assert r.status_code == 422, entry


def test_add_dirs_must_be_absolute(rt, tmp_path):
    assert _put(sandbox={"add_dirs": ["relative/dir"]}).status_code == 422
    assert _put(sandbox={"add_dirs": ["/a/../b"]}).status_code == 422
    r = _put(sandbox={"add_dirs": [str(tmp_path / "intake 3")]})
    assert r.status_code == 200 and r.json()["sandbox"]["add_dirs"] == [str(tmp_path / "intake 3")]


def test_caps_must_be_positive(rt):
    assert _put(sandbox={"http": {"base": "http://127.0.0.1:1", "allow": [], "max_body": 0}}).status_code == 422
    r = _put(sandbox={"http": {"base": "http://127.0.0.1:1", "allow": [], "max_body": 1_000_000}})
    assert r.status_code == 200 and r.json()["sandbox"]["http"]["max_body"] == 1_000_000


def test_unsandboxed_tasks_launch_as_before(rt):
    _put()
    client.post("/tasks/job-1/start")
    assert rt.spawned["sandbox"] is None


def test_stop_and_delete_remove_the_launch_files(rt, tmp_path):
    _put(sandbox=SPEC)
    for verb in ("stop", "delete"):
        d = spawner.sandbox_dir("job-1")
        d.mkdir(parents=True, exist_ok=True)
        (d / "http.json").write_text("{}")
        assert client.post("/tasks/job-1/start").status_code == 200
        if verb == "stop":
            assert client.post("/tasks/job-1/stop").status_code == 200
        else:
            assert client.delete("/tasks/job-1").status_code == 200
        assert not d.exists(), verb
    assert tmp_path in spawner.SANDBOX_DIR.parents   # the fixture kept all of that in tmp


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
    assert "--tools Read,Glob,Grep" in bare and "mcp__sandbox__http" not in bare and "--add-dir" not in bare
    _, extra = spawner.sandbox_launch("job-6", {"add_dirs": ["/data/intake 3", "/data/deals/7"]})
    assert "--restricted --add-dir '/data/intake 3' --add-dir /data/deals/7 --tools" in extra
    with pytest.raises(ValueError):
        spawner.sandbox_launch("job-7", {"tools": ["Bash"]})


def test_spawn_command_quotes_everything_from_the_api(monkeypatch, tmp_path):
    """cwd, home and model are API input; they go into the tmux command as
    single words, and the whole script survives tmux's own `sh -c`."""
    runs = []
    monkeypatch.setattr(spawner.subprocess, "run",
                        lambda argv, **k: runs.append(argv) or type("R", (), {"returncode": 1, "stdout": ""})())
    cwd = str(tmp_path / "deal 7's docs$(id)")
    assert spawner.spawn_tmux("job-q", ["/p/with space"], model="opus'x", cwd=cwd, home="/h o/me") is False
    argv = runs[0]
    assert argv[:4] == ["tmux", "new-session", "-d", "-s"]
    bash, dash_lc, script = shlex.split(argv[-1])
    assert (bash, dash_lc) == ("bash", "-lc")
    words = shlex.split(script.replace("\\\n", " "))
    assert f"cd" in words and words[words.index("cd") + 1] == cwd
    assert "export" in words and f"HOME=/h o/me" in words
    assert words[words.index("--model") + 1] == "opus'x"
    assert words[words.index("--plugin-dir") + 1] == "/p/with space"
    assert "$(id)" not in script.replace(shlex.quote(cwd), "")


def test_spawn_reports_failure_when_the_session_dies_during_startup(monkeypatch):
    monkeypatch.setattr(spawner.subprocess, "run",
                        lambda argv, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    monkeypatch.setattr(spawner, "wait_for_ready", lambda session, timeout=45.0: False)
    monkeypatch.setattr(spawner, "is_tmux_alive", lambda task_id: False)
    assert spawner.spawn_tmux("job-d", [], cwd="/no/such/dir") is False


# --------------------------- the http tool ---------------------------

ALLOW = {"base": "http://127.0.0.1:1",
         "allow": [("POST", "/api/deals/7/extractions/3"), ("GET", "/api/deals/7/docs/*")]}


@pytest.mark.parametrize("method,path,ok", [
    ("POST", "/api/deals/7/extractions/3", True),
    ("post", "/api/deals/7/extractions/3", True),
    ("POST", "/api/deals/7/extractions/3/extra", False),    # exact pattern: nothing under it
    ("POST", "/api/deals/7/extractions/30", False),
    ("POST", "/api/deals/7/extractions/3/", False),
    ("GET", "/api/deals/7/extractions/3", False),           # wrong method
    ("GET", "/api/deals/7/docs", True),                     # /* pattern: the root …
    ("GET", "/api/deals/7/docs/12/text", True),             # … and everything under it
    ("GET", "/api/deals/7/docs2", False),
    ("GET", "/api/deals/7/docs/../../8/docs", False),
    ("GET", "/api/deals/7/docs/./x", False),
    ("HEAD", "/api/deals/7/docs", False),
    ("OPTIONS", "/api/deals/7/docs", False),
    ("POST", "/api/deals/7/extractions/3?notify=1", False),  # no query strings
    ("POST", "/api/deals/7/extractions/3#x", False),
    ("POST", "/api/deals/7/extractions/3/../../../notify", False),
    ("POST", "/api/deals/7/extractions/3/%2e%2e/%2e%2e/notify", False),
    ("POST", "/api/deals/7/extractions/3/%252e%252e/notify", False),
    ("POST", "/api/deals/7/extractions/3/..;/notify", False),
    ("POST", "/api/deals/7/extractions/3/..%00/notify", False),
    ("POST", "/api/deals/7/extractions/3%5c..%5cnotify", False),
    ("POST", "/api/deals/7/extractions/3\\..\\notify", False),
    ("POST", "/api/deals/7/extractions/3 ", False),
    ("POST", "/api/deals/7/extractions/3\n", False),
    ("POST", "/api/deals/7/extractions/3/é", False),
    ("POST", "http://evil.example/api/deals/7/extractions/3", False),
    ("POST", "//evil.example/api/deals/7/extractions/3", False),
    ("POST", "api/deals/7/extractions/3", False),
    ("POST", "", False),
])
def test_http_tool_allowlist(method, path, ok):
    assert (sandbox_http.allowed(ALLOW, method, path) is None) is ok


def test_an_empty_allowlist_refuses_everything():
    cfg = {"base": "http://127.0.0.1:1", "allow": []}
    assert sandbox_http.allowed(cfg, "GET", "/") is not None
    assert sandbox_http.allowed(cfg, "GET", "/api") is not None


def test_refused_calls_never_leave_the_box(monkeypatch):
    sent = []
    monkeypatch.setattr(sandbox_http.urllib.request, "build_opener", lambda *a: sent.append(a))
    cfg = {"base": "http://127.0.0.1:1", "allow": [("POST", "/api/x")], "headers": {}}
    assert sandbox_http.call(cfg, "POST", "/api/notify", {"a": 1}).startswith("REFUSED")
    assert sandbox_http.call(cfg, "POST", "/api/x?y=1", {"a": 1}).startswith("REFUSED")
    assert sent == []


def test_load_rejects_a_bad_config_file(tmp_path):
    p = tmp_path / "http.json"
    p.write_text(json.dumps({"base": "http://127.0.0.1:1/", "allow": [["POST", "/*"]]}))
    with pytest.raises(ValueError):
        sandbox_http.load(str(p))
    p.write_text(json.dumps({"base": "http://127.0.0.1:1/", "allow": [["post", "/api/x"]]}))
    assert sandbox_http.load(str(p))["allow"] == [("POST", "/api/x")] and sandbox_http.load(str(p))["base"].endswith(":1")


@pytest.fixture()
def server():
    """A local HTTP server that records exactly what arrived."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def _any(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                         "ctype": self.headers.get("Content-Type"), "body": self.rfile.read(n)})
            if self.path.startswith("/go-away"):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.end_headers()
                return
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"x" * 40_000 if self.path.endswith("/big") else b'{"ok": true}')

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_allowed_calls_go_out_exactly_as_checked_with_the_headers_added(server):
    base, seen = server
    cfg = {"base": base, "allow": [("POST", "/api/deals/7/extractions/3"), ("GET", "/api/x/*")],
           "headers": {"Authorization": "Bearer s3cret"}}
    out = sandbox_http.call(cfg, "post", "/api/deals/7/extractions/3", {"rows": [1, 2]})
    assert out.startswith("HTTP 201") and '"ok": true' in out
    assert seen[-1] == {"method": "POST", "path": "/api/deals/7/extractions/3", "auth": "Bearer s3cret",
                        "ctype": "application/json", "body": b'{"rows": [1, 2]}'}
    assert sandbox_http.call(cfg, "GET", "/api/x/big").startswith("HTTP 201")
    assert seen[-1]["auth"] == "Bearer s3cret" and seen[-1]["body"] == b""


def test_caps_and_no_redirects(server):
    base, seen = server
    cfg = {"base": base, "allow": [("POST", "/api/x"), ("GET", "/api/x/*"), ("GET", "/go-away")],
           "headers": {}, "max_body": 100, "max_reply": 1000}
    assert sandbox_http.call(cfg, "POST", "/api/x", {"pad": "y" * 200}).startswith("REFUSED: body is")
    assert not any(s["path"] == "/api/x" for s in seen)
    small = sandbox_http.call(cfg, "POST", "/api/x", {"pad": "y" * 50})
    assert small.startswith("HTTP 201")
    big = sandbox_http.call(cfg, "GET", "/api/x/big")
    assert big.endswith("…(truncated)") and len(big) < 1200
    assert sandbox_http.call(cfg, "GET", "/go-away").startswith("HTTP 302")
    assert not any(s["path"] == "/elsewhere" for s in seen)
