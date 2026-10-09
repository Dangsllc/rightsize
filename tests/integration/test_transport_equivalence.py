"""The same system must score identically however it is reached."""

import asyncio
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from rightsize_core.integrity.dataset import load_tasks
from rightsize_core.systems.declarative import DeclarativeSystem
from rightsize_core.systems.native import NativeHTTPSystem, NativeMCPSystem
from rightsize_core.systems.plugin import PluginSystem
from rightsize_pack_compliance.pack import PACK
from rightsize_reference_systems.registry import make
from rightsize_reference_systems.server import http_app, mcp_server

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "configs/systems/examples"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def bm25():
    return make("bm25", ROOT, "m1")


@pytest.fixture(scope="module")
def http_server(bm25):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(http_app(bm25), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/rightsize/v1/info", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def _cfg(name: str, base_url: str | None = None) -> dict:
    cfg = yaml.safe_load((EXAMPLES / name).read_text())
    if base_url:
        cfg["base_url"] = base_url
    return cfg


async def _hits(system, tasks):
    await system.preflight()
    await system.setup()
    try:
        outs = [await system.run(t.task_type, t.inputs) for t in tasks]
    finally:
        await system.teardown()
        await system.aclose()
    assert all(o.error is None for o in outs), [o.error for o in outs if o.error]
    return [[(h["citation"], round(h["score"], 6)) for h in o.prediction["hits"]] for o in outs]


def test_four_transports_agree(bm25, http_server):
    tasks = load_tasks(ROOT, "m1", "public", ["retrieval"])[:8]

    async def go():
        direct = await _hits(PluginSystem(bm25), tasks)
        native_http = await _hits(NativeHTTPSystem(http_server), tasks)
        native_mcp = await _hits(NativeMCPSystem(mcp_server(bm25)), tasks)
        decl_http = await _hits(DeclarativeSystem(_cfg("bm25-declarative-http.yaml", http_server), PACK.transforms), tasks)
        decl_mcp = await _hits(
            DeclarativeSystem(_cfg("bm25-declarative-mcp.yaml"), PACK.transforms, mcp_server_override=mcp_server(bm25)),
            tasks,
        )
        return direct, native_http, native_mcp, decl_http, decl_mcp

    direct, *others = asyncio.run(go())
    for o in others:
        assert o == direct
