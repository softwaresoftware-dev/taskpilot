"""The one door a sandboxed agent has to the outside: an HTTP tool limited to
the method + path prefixes its task allows, on one base URL.

Spawned by Claude Code as a stdio MCP server for a sandboxed task (see
spawner.sandbox_files). Its config file — base URL, allowed routes, and the
headers to add (the caller's bearer token) — lives in the task's private dir,
outside the agent's working directory, so the agent can use the credential
without being able to read it.

    TASKPILOT_SANDBOX_HTTP=/path/to/http.json python sandbox_http.py
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

MAX_BODY = 64 * 1024        # what the agent may send
MAX_REPLY = 32 * 1024       # what it gets back


def load(path: str | None = None) -> dict:
    with open(path or os.environ["TASKPILOT_SANDBOX_HTTP"]) as f:
        cfg = json.load(f)
    cfg["base"] = cfg["base"].rstrip("/")
    cfg["allow"] = [(m.upper(), p) for m, p in cfg.get("allow", [])]
    return cfg


def allowed(cfg: dict, method: str, path: str) -> str | None:
    """None when the call may go out, else why not."""
    if not path.startswith("/") or "//" in path or "\\" in path:
        return "path must be a plain absolute path on the task's server"
    parts = urllib.parse.urlsplit(path)
    if parts.scheme or parts.netloc:
        return "path must not carry a host"
    clean = urllib.parse.unquote(parts.path)
    if "/../" in clean + "/" or clean.endswith("/.."):
        return "path must not climb with .."
    for m, prefix in cfg["allow"]:
        if m == method.upper() and (clean == prefix or clean.startswith(prefix.rstrip("/") + "/")
                                    or (prefix.endswith("/") and clean.startswith(prefix))):
            return None
    return f"{method.upper()} {clean} is not a route this task may call"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # a redirect could leave the allowed host
        return None


def call(cfg: dict, method: str, path: str, body: dict | list | None = None) -> str:
    why = allowed(cfg, method, path)
    if why:
        return f"REFUSED: {why}. Allowed: " + ", ".join(f"{m} {p}" for m, p in cfg["allow"])
    data = None
    headers = dict(cfg.get("headers") or {})
    if body is not None:
        data = json.dumps(body).encode()
        if len(data) > MAX_BODY:
            return f"REFUSED: body over {MAX_BODY} bytes"
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(cfg["base"] + path, data=data, headers=headers, method=method.upper())
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=60) as r:
            status, text = r.status, r.read(MAX_REPLY + 1).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read(MAX_REPLY + 1).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"ERROR: {type(e).__name__}: {e}"
    if len(text) > MAX_REPLY:
        text = text[:MAX_REPLY] + "\n…(truncated)"
    return f"HTTP {status}\n{text}"


def main() -> None:
    cfg = load()
    mcp = FastMCP("sandbox")
    routes = "; ".join(f"{m} {p}" for m, p in cfg["allow"]) or "none"

    @mcp.tool(description=f"Call this task's server ({cfg['base']}). Only these routes are allowed: {routes}. "
                          "`path` is the path only (e.g. /api/x), `body` is a JSON object for POST/PUT/PATCH. "
                          "Authentication is added for you.")
    def http(method: str, path: str, body: dict | list | None = None) -> str:
        return call(cfg, method, path, body)

    mcp.run()


if __name__ == "__main__":
    main()
