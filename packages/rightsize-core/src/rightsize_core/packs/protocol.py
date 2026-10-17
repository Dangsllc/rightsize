"""What a task pack must provide. The core never knows what a 'control' or a 'citation' is.

Scoring is split in two so the core can bootstrap any metric:

* ``score_item`` turns one task + one prediction into unit records (a retrieval query is one
  unit; a manual with 30 controls is 30 units, maybe at two levels).
* ``aggregate`` turns any bag of unit records into metric values. The core resamples clusters
  of units and calls ``aggregate`` again to get confidence intervals.

A pack may also provide ``item_view``, which explains one scored item to a person or an agent
(what was asked, how the expected answer was built, what the system said) as generic blocks the
core can render without knowing the domain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

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


Mark = Literal["ok", "bad", "warn", "info", ""]


class Highlight(BaseModel):
    """A span of a document block's text, e.g. evidence a system quoted."""

    start: int
    end: int
    label: str = ""
    mark: Mark = ""


class ViewBlock(BaseModel):
    """One piece of an item view. `kind` says which fields are used:
    fields (label/value pairs), table (columns, rows, a mark per row), document (text with
    highlights), notes (plain sentences). Every string is plain text; renderers escape it."""

    kind: Literal["fields", "table", "document", "notes"]
    title: str | None = None
    fields: list[tuple[str, str]] = []
    columns: list[str] = []
    rows: list[list[str]] = []
    row_marks: list[Mark] = []
    text: str = ""
    highlights: list[Highlight] = []
    notes: list[str] = []


class ItemView(BaseModel):
    task_id: str
    title: str
    summary: str
    mark: Mark = ""
    blocks: list[ViewBlock] = []


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
    # (root, task, outcome dict from results.jsonl or None, the item's unit records) -> ItemView
    item_view: Callable[[Path, Task, dict | None, list[UnitRecord]], ItemView] | None = None


class TaskPack(Protocol):
    name: str
    version: str
    task_types: dict[str, TaskTypeSpec]

    def normalize_citation(self, raw: str) -> str | None: ...
