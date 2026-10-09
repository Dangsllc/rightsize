"""In-process systems: a Python object with async `run(task_type, inputs, model, params)` and
`info()`. Used for reference systems in tests and for private diagnostic plugins."""

from __future__ import annotations

import importlib
import time
from typing import Any

from rightsize_core.schema import Outcome, OutcomeError, SystemInfo
from rightsize_core.systems.base import System


class PluginSystem(System):
    transport = "plugin"

    def __init__(self, impl: Any, name: str | None = None, trusted: bool = True) -> None:
        super().__init__()
        self.impl = impl
        self.trusted = trusted
        self.name = name or type(impl).__name__

    async def info(self) -> SystemInfo:
        info = self.impl.info()
        self.name = info.name
        return info

    async def preflight(self) -> None:
        if hasattr(self.impl, "preflight"):
            res = self.impl.preflight()
            if hasattr(res, "__await__"):
                await res

    async def run(self, task_type: str, inputs: dict[str, Any]) -> Outcome:
        t0 = time.monotonic()
        try:
            out = await self.impl.run(task_type, inputs, self.model, self.params)
        except Exception as e:  # noqa: BLE001
            return Outcome(error=OutcomeError(kind="error", message=repr(e)[:500]),
                           latency_ms=int((time.monotonic() - t0) * 1000))
        if out.latency_ms is None:
            out.latency_ms = int((time.monotonic() - t0) * 1000)
        return out

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        if hasattr(self.impl, "self_report"):
            d["self_report"] = self.impl.self_report()
        return d


def load_object(spec: str) -> Any:
    """'package.module:factory' -> factory(). Extra import roots come from RIGHTSIZE_PLUGIN_PATH."""
    import os
    import sys

    for extra in filter(None, os.environ.get("RIGHTSIZE_PLUGIN_PATH", "").split(os.pathsep)):
        if extra not in sys.path:
            sys.path.insert(0, os.path.abspath(extra))
    mod, _, attr = spec.partition(":")
    obj = getattr(importlib.import_module(mod), attr)
    return obj() if callable(obj) else obj
