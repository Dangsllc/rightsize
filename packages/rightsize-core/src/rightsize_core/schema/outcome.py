"""What a system returns for one task, plus what it says about itself."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorKind = Literal["error", "refusal", "timeout", "unsupported", "mapping"]


class UsageRecord(BaseModel):
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    latency_ms: int | None = None
    estimated: bool = False
    call_role: str | None = None


class OutcomeError(BaseModel):
    kind: ErrorKind
    message: str


class Outcome(BaseModel):
    """One system response. `prediction` is validated against the pack's prediction model."""

    model_config = ConfigDict(extra="forbid")

    prediction: dict[str, Any] | None = None
    usage: list[UsageRecord] | None = None
    params_sent: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = None
    latency_kind: Literal["model", "end_to_end"] = "end_to_end"
    error: OutcomeError | None = None
    path: str | None = None
    raw: Any = None


class SystemInfo(BaseModel):
    """`GET /rightsize/v1/info` / MCP `rightsize_info`."""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str | None = None
    protocol: str = "rightsize/v1"
    task_types: list[str]
    granularity: dict[str, Literal["spec", "group", "document"]] = Field(default_factory=dict)
    supports_model_param: bool = False
    models: list[str] = Field(default_factory=list)
    efforts: list[str] = Field(default_factory=list)
    reports_usage: bool = False
    deterministic: bool = False


class RunRequest(BaseModel):
    """`POST /rightsize/v1/run` / MCP `rightsize_run`."""

    task_type: str
    inputs: dict[str, Any]
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
