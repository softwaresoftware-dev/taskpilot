"""The one door a sandboxed agent has to the outside: an HTTP tool limited to
the routes its task allows, on one base URL.

Spawned by Claude Code as a stdio MCP server for a sandboxed task (see
spawner.sandbox_launch). Its config file — base URL, allowed routes, and the
headers to add (the caller's bearer token) — lives in the task's private dir,
outside the agent's working directory, so the agent can use the credential
without being able to read it.

    TASKPILOT_SANDBOX_HTTP=/path/to/http.json python sandbox_http.py

Allowed routes are [method, pattern] pairs. A pattern matches the path exactly;
a pattern ending in "/*" also matches every path under it. The path grammar is
deliberately small (see PATH_RE): no percent-encoding, query strings, ";"
parameters or backslashes, so what the allowlist checks is byte-for-byte what
goes on the wire and the server has nothing to re-interpret.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_MAX_BODY = 64 * 1024        # what the agent may send
DEFAULT_MAX_REPLY = 32 * 1024       # what it gets back
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
# A plain absolute path: segments of unreserved characters, no empty or dot segments.
PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_~-][A-Za-z0-9_~.-]*(?:/[A-Za-z0-9_~-][A-Za-z0-9_~.-]*)*/?)?$")
PATTERN_RE = re.compile(r"^/(?:[A-Za-z0-9_~-][A-Za-z0-9_~.-]*(?:/[A-Za-z0-9_~-][A-Za-z0-9_~.-]*)*)?(?:/\*)?$")


def check_pattern(method: str, pattern: str) -> str | None:
    """None when [method, pattern] is a usable allow entry, else why not."""
    if method.upper() not in METHODS:
        return f"method {method!r} is not one of {', '.join(sorted(METHODS))}"
    if not PATTERN_RE.match(pattern):
        return f"pattern {pattern!r} must be a plain absolute path, optionally ending in /*"
    if pattern in ("/*",):
        return "pattern /* would allow every route; list the ones the task needs"
    return None


def load(path: str | None = None) -> dict:
    with open(path or os.environ["TASKPILOT_SANDBOX_HTTP"]) as f:
        cfg = json.load(f)
    cfg["base"] = cfg["base"].rstrip("/")
    cfg["allow"] = [(m.upper(), p) for m, p in cfg.get("allow", [])]
    for m, p in cfg["allow"]:
        why = check_pattern(m, p)
        if why:
            raise ValueError(f"bad allow entry [{m}, {p}]: {why}")
    return cfg


def _matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/*"):
        root = pattern[:-2]
        return path == root or path.startswith(root + "/")
    return path == pattern


def allowed(cfg: dict, method: str, path: str) -> str | None:
    """None when the call may go out, else why not."""
    method = method.upper()
    if method not in METHODS:
        return f"method must be one of {', '.join(sorted(METHODS))}"
    if not PATH_RE.match(path):
        return ("path must be a plain absolute path on the task's server: letters, digits, "
                "/ . _ ~ - only, no query string, no encoding, no . or .. segments")
    for m, pattern in cfg["allow"]:
        if m == method and _matches(pattern, path):
            return None
    return f"{method} {path} is not a route this task may call"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # a redirect could leave the allowed host
        return None


def call(cfg: dict, method: str, path: str, body: dict | list | None = None) -> str:
    why = allowed(cfg, method, path)
    if why:
        return f"REFUSED: {why}. Allowed: " + ", ".join(f"{m} {p}" for m, p in cfg["allow"])
    max_body = int(cfg.get("max_body") or DEFAULT_MAX_BODY)
    max_reply = int(cfg.get("max_reply") or DEFAULT_MAX_REPLY)
    data = None
    headers = dict(cfg.get("headers") or {})
    if body is not None:
        data = json.dumps(body).encode()
        if len(data) > max_body:
            return f"REFUSED: body is {len(data)} bytes, over this task's limit of {max_body}"
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(cfg["base"] + path, data=data, headers=headers, method=method.upper())
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=60) as r:
            status, text = r.status, r.read(max_reply + 1).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read(max_reply + 1).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return f"ERROR: {type(e).__name__}: {e}"
    if len(text) > max_reply:
        text = text[:max_reply] + "\n…(truncated)"
    return f"HTTP {status}\n{text}"


def main() -> None:
    from mcp.server.fastmcp import FastMCP   # here so the daemon can import the checks without mcp
    cfg = load()
    mcp = FastMCP("sandbox")
    routes = "; ".join(f"{m} {p}" for m, p in cfg["allow"]) or "none"

    @mcp.tool(description=f"Call this task's server ({cfg['base']}). Only these routes are allowed: {routes} "
                          "(a route ending in /* includes the paths under it). "
                          "`path` is the path only (e.g. /api/x), `body` is a JSON object for POST/PUT/PATCH. "
                          "Authentication is added for you.")
    def http(method: str, path: str, body: dict | list | None = None) -> str:
        return call(cfg, method, path, body)

    mcp.run()


if __name__ == "__main__":
    main()
