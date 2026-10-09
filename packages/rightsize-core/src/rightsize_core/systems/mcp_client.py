"""A thin, long-lived MCP client connection (stdio command, streamable-HTTP URL, or in-process
server object for tests). Start and close it from the same asyncio task."""

from __future__ import annotations

import json
import os
import shlex
from typing import Any


class MCPToolError(RuntimeError):
    pass


class MCPConnection:
    def __init__(self, server: Any, env: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None, read_timeout_s: float | None = 1800) -> None:
        self.server = server
        self.env = env
        self.headers = headers
        self.read_timeout_s = read_timeout_s
        self._client = None

    def _target(self) -> Any:
        from mcp import StdioServerParameters

        s = self.server
        if not isinstance(s, str):
            return s  # in-process MCPServer, or a Transport
        if s.startswith(("http://", "https://")):
            if self.headers:
                import httpx2  # the SDK's HTTP client
                from mcp.client.streamable_http import streamable_http_client

                return streamable_http_client(s, http_client=httpx2.AsyncClient(headers=self.headers))
            return s
        parts = shlex.split(s)
        return StdioServerParameters(command=parts[0], args=parts[1:],
                                     env={**os.environ, **(self.env or {})})

    async def start(self) -> None:
        if self._client is not None:
            return
        from mcp import Client

        self._client = Client(self._target(), read_timeout_seconds=self.read_timeout_s)
        await self._client.__aenter__()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def list_tools(self) -> list[str]:
        await self.start()
        res = await self._client.list_tools()
        return [t.name for t in res.tools]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Call a tool and return its result as data (structured content, else parsed text)."""
        await self.start()
        res = await self._client.call_tool(tool, args)
        if getattr(res, "is_error", False):
            text = " ".join(getattr(c, "text", "") for c in (res.content or []))
            raise MCPToolError(f"{tool}: {text[:500]}")
        sc = getattr(res, "structured_content", None)
        if sc is not None:
            if isinstance(sc, dict) and set(sc) == {"result"}:
                return sc["result"]
            return sc
        texts = [getattr(c, "text", None) for c in (res.content or [])]
        texts = [t for t in texts if t]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]
        return texts

    async def call_json(self, tool: str, args: dict[str, Any]) -> Any:
        return await self.call(tool, args)
