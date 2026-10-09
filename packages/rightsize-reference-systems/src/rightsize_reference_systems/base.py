"""Reference systems: small, honest baselines that speak rightsize/v1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from rightsize_core.schema import Outcome, SystemInfo


class ReferenceSystem(ABC):
    @abstractmethod
    def info(self) -> SystemInfo: ...

    @abstractmethod
    async def run(self, task_type: str, inputs: dict[str, Any], model: str | None,
                  params: dict[str, Any]) -> Outcome: ...
