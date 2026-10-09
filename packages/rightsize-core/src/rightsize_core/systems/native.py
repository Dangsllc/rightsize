"""Systems that speak rightsize/v1 themselves, over HTTP or MCP."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from rightsize_core.schema import Outcome, OutcomeError, SystemInfo
from rightsize_core.systems.base import System, SystemPreflightError


class NativeHTTPSystem(System):
    transport = "native-http"

    def __init__(self, base_url: str, headers: dict[str, str] | None = None, trusted: bool = False,
                 timeout_s: float = 1800, poll_every_s: float = 2.0, name: str | None = None) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        self.trusted = trusted
        self.timeout_s = timeout_s
        self.poll_every_s = poll_every_s
        self.name = name or self.base_url
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10), headers=self.headers)
        self._info: SystemInfo | None = None

    async def info(self) -> SystemInfo:
        if self._info is None:
            r = await self._client.get(f"{self.base_url}/rightsize/v1/info")
            r.raise_for_status()
            self._info = SystemInfo.model_validate(r.json())
            self.name = self._info.name
        return self._info

    async def preflight(self) -> None:
        try:
            await self.info()
        except Exception as e:
            raise SystemPreflightError(f"{self.base_url}/rightsize/v1/info failed: {e}") from e

    async def run(self, task_type: str, inputs: dict[str, Any]) -> Outcome:
        body = {"task_type": task_type, "inputs": inputs, "model": self.model, "params": self.params}
        t0 = time.monotonic()
        try:
            r = await self._client.post(f"{self.base_url}/rightsize/v1/run", json=body)
            if r.status_code == 202:
                loc = r.headers.get("Location")
                if not loc:
                    raise RuntimeError("202 without Location header")
                url = loc if loc.startswith("http") else f"{self.base_url}{loc}"
                deadline = t0 + self.timeout_s
                while True:
                    await asyncio.sleep(self.poll_every_s)
                    r = await self._client.get(url)
                    if r.status_code != 202:
                        break
                    if time.monotonic() > deadline:
                        return Outcome(error=OutcomeError(kind="timeout", message=f"polling {url}"))
            r.raise_for_status()
            out = Outcome.model_validate(r.json())
        except httpx.HTTPStatusError as e:
            return Outcome(error=OutcomeError(kind="error", message=f"HTTP {e.response.status_code}: {e.response.text[:300]}"),
                           latency_ms=int((time.monotonic() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            return Outcome(error=OutcomeError(kind="error", message=repr(e)[:500]),
                           latency_ms=int((time.monotonic() - t0) * 1000))
        if out.latency_ms is None:
            out.latency_ms = int((time.monotonic() - t0) * 1000)
            out.latency_kind = "end_to_end"
        return out

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "base_url": self.base_url}

    async def aclose(self) -> None:
        await self._client.aclose()


class NativeMCPSystem(System):
    """A system exposing the rightsize/v1 contract as MCP tools `rightsize_info` and `rightsize_run`.

    `server` is either a URL (streamable HTTP) or a command line for a stdio server.
    """

    transport = "native-mcp"

    def __init__(self, server: str, trusted: bool = False, env: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None, name: str | None = None) -> None:
        super().__init__()
        from rightsize_core.systems.mcp_client import MCPConnection

        self.server = server
        self.trusted = trusted
        self.name = name or server
        self.conn = MCPConnection(server, env=env, headers=headers)
        self._info: SystemInfo | None = None

    async def info(self) -> SystemInfo:
        if self._info is None:
            data = await self.conn.call_json("rightsize_info", {})
            self._info = SystemInfo.model_validate(data)
            self.name = self._info.name
        return self._info

    async def preflight(self) -> None:
        try:
            await self.conn.start()
            await self.info()
        except Exception as e:
            raise SystemPreflightError(f"MCP server {self.server!r} rightsize_info failed: {e}") from e

    async def run(self, task_type: str, inputs: dict[str, Any]) -> Outcome:
        t0 = time.monotonic()
        args = {"task_type": task_type, "inputs": inputs, "model": self.model, "params": self.params}
        try:
            data = await self.conn.call_json("rightsize_run", args)
            out = Outcome.model_validate(data)
        except Exception as e:  # noqa: BLE001
            return Outcome(error=OutcomeError(kind="error", message=repr(e)[:500]),
                           latency_ms=int((time.monotonic() - t0) * 1000))
        if out.latency_ms is None:
            out.latency_ms = int((time.monotonic() - t0) * 1000)
        return out

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "server": self.server}

    async def aclose(self) -> None:
        await self.conn.close()


def outcome_json(o: Outcome) -> str:
    return json.dumps(o.model_dump(mode="json"))
