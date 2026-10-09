"""What a task pack must provide. The core never knows what a 'control' or a 'citation' is.

Scoring is split in two so the core can bootstrap any metric:

* ``score_item`` turns one task + one prediction into unit records (a retrieval query is one
  unit; a manual with 30 controls is 30 units, maybe at two levels).
* ``aggregate`` turns any bag of unit records into metric values. The core resamples clusters
  of units and calls ``aggregate`` again to get confidence intervals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from rightsize_core.schema import Task


class UnitRecord(BaseModel):
    """One scored unit. `level` lets a pack score the same item at several granularities."""

    task_id: str
    unit_id: str
    cluster: str
    level: str = "default"
    strata: dict[str, str] = {}
    gold: Any = None
    pred: Any = None
    outcome: str = "predicted"  # predicted | no_prediction | abstained | error | refusal
    confidence: float | None = None
    values: dict[str, float] = {}
    extra: dict[str, Any] = {}


@dataclass
class TaskTypeSpec:
    name: str
    input_model: type[BaseModel]
    prediction_model: type[BaseModel]
    score_item: Callable[[Task, BaseModel | None, str], list[UnitRecord]]
    aggregate: Callable[[list[UnitRecord]], dict[str, float]]
    primary_metric: str
    levels: list[str] = field(default_factory=lambda: ["default"])
    floors: Callable[[list[Task]], dict[str, dict[str, float]]] | None = None
    higher_is_better: dict[str, bool] = field(default_factory=dict)


class TaskPack(Protocol):
    name: str
    version: str
    task_types: dict[str, TaskTypeSpec]

    def normalize_citation(self, raw: str) -> str | None: ...
