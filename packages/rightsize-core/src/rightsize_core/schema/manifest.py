"""Everything needed to reproduce or explain a run."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DatasetRef(BaseModel):
    suite: str
    split: str
    files: dict[str, str]  # path -> sha256
    canary: str | None = None
    task_count: int = 0


class RunManifest(BaseModel):
    run_id: str
    rightsize_version: str
    rightsize_git_sha: str | None = None
    protocol: str = "rightsize/v1"
    started_at: str
    ended_at: str | None = None
    status: Literal["running", "ok", "degraded", "aborted"] = "running"
    dataset: DatasetRef
    system: dict[str, Any]  # config hash, transport, info() response, self_report
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    repeats: int = 1
    task_types: list[str] = Field(default_factory=list)
    pricing: dict[str, Any] | None = None
    host: dict[str, Any] = Field(default_factory=dict)
    sweep_id: str | None = None
    notes: list[str] = Field(default_factory=list)
