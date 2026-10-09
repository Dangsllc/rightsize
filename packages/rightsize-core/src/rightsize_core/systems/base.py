"""The one interface every system under test is reached through.

Three implementations: ``native`` (the system speaks rightsize/v1 over HTTP or MCP), ``declarative``
(a YAML config maps our tasks onto the system's own HTTP or MCP API), and ``plugin`` (a Python
object, used for in-process diagnostics).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from rightsize_core.schema import Outcome, SystemInfo


class SystemPreflightError(RuntimeError):
    """Raised when a system is not in a state where its scores would mean anything."""


class System(ABC):
    name: str = "system"
    transport: str = "unknown"
    trusted: bool = False

    def __init__(self) -> None:
        self.model: str | None = None
        self.params: dict[str, Any] = {}

    def configure(self, model: str | None = None, params: dict[str, Any] | None = None) -> None:
        self.model = model
        self.params = dict(params or {})

    @abstractmethod
    async def info(self) -> SystemInfo: ...

    async def preflight(self) -> None:
        """Raise SystemPreflightError if the system must not be scored."""

    @abstractmethod
    async def run(self, task_type: str, inputs: dict[str, Any]) -> Outcome: ...

    async def setup(self) -> None:
        """Run once before the first task."""

    async def teardown(self) -> None:
        """Run once after the last task, even after a failure."""

    def describe(self) -> dict[str, Any]:
        """Static description recorded in the manifest (config hash, transport, endpoint)."""
        return {"name": self.name, "transport": self.transport, "trusted": self.trusted}

    async def aclose(self) -> None:
        pass
