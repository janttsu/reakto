"""A minimal MCP client over stdio, enough to call a local mail server's tools.

reakto starts the server command as a child process on this machine and
speaks JSON-RPC 2.0 to it, one JSON object per line. No network, no extra
dependencies. Only ``initialize``, ``tools/list`` and ``tools/call`` are used.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
from typing import Any

from reakto import __version__

PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, command: str, *, timeout: float = 600.0, env: dict[str, str] | None = None):
        self.command = command
        self.timeout = timeout
        self.env = env
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 0
        self.tools: dict[str, dict] = {}

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> MCPClient:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        argv = shlex.split(os.path.expanduser(self.command))
        if not argv:
            raise MCPError("empty MCP command")
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, bufsize=0,
            )
        except OSError as e:
            raise MCPError(f"cannot start MCP server {argv[0]!r}: {e}") from e
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "reakto", "version": __version__},
        })
        self._notify("notifications/initialized")
        listed = self._request("tools/list", {})
        self.tools = {t["name"]: t for t in listed.get("tools", []) if "name" in t}

    def close(self) -> None:
        p = self._proc
        self._proc = None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            p.kill()

    # -- protocol ------------------------------------------------------------
    def _send(self, msg: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPError(f"MCP server went away: {e}") from e

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _readline(self) -> bytes:
        assert self._proc is not None and self._proc.stdout is not None
        out = self._proc.stdout
        ready, _, _ = select.select([out], [], [], self.timeout)
        if not ready:
            raise MCPError(f"MCP server did not answer within {self.timeout:.0f}s")
        line = out.readline()
        if not line:
            raise MCPError("MCP server closed the connection")
        return line

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            line = self._readline().strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log line on stdout
            if msg.get("id") != rid:
                if "method" in msg and "id" in msg:
                    # A request from the server (sampling, roots…): decline politely.
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "not supported"}})
                continue
            if "error" in msg:
                err = msg["error"]
                raise MCPError(f"{method}: {err.get('message', err)}")
            return msg.get("result") or {}

    # -- tools -----------------------------------------------------------------
    def call(self, name: str, **arguments: Any) -> Any:
        """Call a tool and return its result as Python data.

        FastMCP servers return JSON text in ``content`` (one item per list
        element) and, in newer versions, ``structuredContent`` as well.
        """
        if self.tools and name not in self.tools:
            raise MCPError(f"the MCP server has no tool {name!r} (has: {', '.join(sorted(self.tools))})")
        res = self._request("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            raise MCPError(f"{name}: {_text(res)[:400]}")
        structured = res.get("structuredContent")
        if isinstance(structured, dict):
            return structured["result"] if set(structured) == {"result"} else structured
        items = []
        for c in res.get("content") or []:
            if c.get("type") != "text":
                continue
            try:
                items.append(json.loads(c["text"]))
            except (json.JSONDecodeError, TypeError, KeyError):
                items.append(c.get("text"))
        if len(items) == 1 and isinstance(items[0], (list, dict)):
            return items[0]
        return items


def _text(res: dict) -> str:
    return " ".join(str(c.get("text", "")) for c in res.get("content") or [] if isinstance(c, dict))
