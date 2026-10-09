"""Serve any reference system over rightsize/v1: HTTP (FastAPI) and MCP (stdio or streamable HTTP)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from rightsize_core.schema import RunRequest

from rightsize_reference_systems.base import ReferenceSystem


def http_app(system: ReferenceSystem) -> FastAPI:
    app = FastAPI(title=f"rightsize/v1 · {system.info().name}")

    @app.get("/rightsize/v1/info")
    def info() -> dict[str, Any]:
        return system.info().model_dump(mode="json")

    @app.post("/rightsize/v1/run")
    async def run(req: RunRequest) -> dict[str, Any]:
        out = await system.run(req.task_type, req.inputs, req.model, req.params)
        return out.model_dump(mode="json")

    return app


def mcp_server(system: ReferenceSystem):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(f"gq-{system.info().name}")

    @mcp.tool(name="rightsize_info", description="Describe this system under the rightsize/v1 protocol.")
    def rightsize_info() -> dict[str, Any]:
        return system.info().model_dump(mode="json")

    @mcp.tool(name="rightsize_run", description="Run one rightsize/v1 task and return an Outcome.")
    async def rightsize_run(task_type: str, inputs: dict[str, Any], model: str | None = None,
                     params: dict[str, Any] | None = None) -> dict[str, Any]:
        out = await system.run(task_type, inputs, model, params or {})
        return out.model_dump(mode="json")

    return mcp
