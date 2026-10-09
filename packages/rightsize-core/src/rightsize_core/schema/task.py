"""The task record: one golden item, stored one per line in JSONL."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Split = Literal["public", "dev", "heldout"]


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    citation: str | None = None
    edition: str | None = None


class Provenance(BaseModel):
    """How the item was made. Labels come from construction, so this is the audit trail."""

    model_config = ConfigDict(extra="allow")

    generator: str
    seed: int | None = None
    seed_artifact: str | None = None
    source: SourceRef | None = None
    generator_llm: str | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    suite: str
    pack: str
    task_type: str
    split: Split
    strata: dict[str, str] = Field(default_factory=dict)
    inputs: dict[str, Any]
    expected: dict[str, Any]
    metric: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    canary: str
