"""Inputs a system receives and predictions it returns, for each compliance task type.

These are the public contract: a system speaking rightsize/v1 sends exactly these shapes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["covered", "partial", "gap", "not_applicable"]
STATUSES: tuple[str, ...] = ("covered", "partial", "gap", "not_applicable")
SCORED_STATUSES: tuple[str, ...] = ("gap", "partial", "covered")  # ordinal, worst to best


class RetrievalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    k: int = 10


class Hit(BaseModel):
    citation: str | None = None
    score: float | None = None
    text: str | None = None
    citation_source: Literal["field", "inferred"] = "field"


class RetrievalPrediction(BaseModel):
    hits: list[Hit] = Field(default_factory=list)


class ControlUnit(BaseModel):
    control_code: str
    title: str
    specification: str
    group: str | None = None
    standard_type: str | None = None


class ControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    document: str
    document_sha256: str
    units: list[ControlUnit]
    groups: dict[str, list[str]] = Field(default_factory=dict)


class Assessment(BaseModel):
    control_code: str
    status: Status | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: str | None = None
    abstained: bool = False


class ControlPrediction(BaseModel):
    assessments: list[Assessment] = Field(default_factory=list)
