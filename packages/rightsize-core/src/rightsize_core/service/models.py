"""What the service layer returns. The Workbench, the MCP server, and `--json` all serialize these,
so a number means the same thing wherever it shows up."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Health = Literal["running", "interrupted", "no_signal", "aborted", "degraded", "ok", "unscored"]
CostState = Literal["known", "zero", "unknown"]
SweepState = Literal["empty", "running", "interrupted", "failed", "cancelled", "stopped", "complete"]


class Headline(BaseModel):
    """One task type's primary metric, with everything needed to read it honestly."""

    task_type: str
    metric: str
    value: float | None = None
    lo: float | None = None
    hi: float | None = None
    n_clusters: int = 0
    low_n: bool = False  # below calibrate's bar for a routing claim
    tiny_n: bool = False  # too few clusters to estimate uncertainty (a perfect score may still be zero-width)
    floors: dict[str, float] = Field(default_factory=dict)  # trivial predictor -> its value on this metric
    clears_floors: bool | None = None  # lower bound above every trivial predictor; None when there are none
    cost_per_task: float | None = None
    cost_total: float | None = None
    cost_state: CostState = "unknown"
    calls: int = 0
    error_rate: float = 0.0
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    latency_kind: str | None = None


class ErrorGroup(BaseModel):
    kind: str
    message: str
    count: int
    first_task_id: str
    hint: str | None = None


class Progress(BaseModel):
    done: int = 0
    total: int = 0
    errors: int = 0
    spend_usd: float = 0.0  # reported spend only
    unknown_cost_calls: int = 0


class RunSummary(BaseModel):
    run_id: str
    path: str
    started_at: str
    ended_at: str | None = None
    suite: str
    split: str
    system: str | None = None
    transport: str | None = None
    trusted: bool = False
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    repeats: int = 1
    task_types: list[str] = Field(default_factory=list)
    sweep_id: str | None = None
    status: str
    health: Health
    health_reason: str = ""
    progress: Progress = Field(default_factory=Progress)
    headlines: list[Headline] = Field(default_factory=list)
    error_groups: list[ErrorGroup] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    max_cost: float | None = None


class RungState(BaseModel):
    rung: str
    model: str | None = None
    status: str  # pending | running | done | failed | cancelled
    run_id: str | None = None
    health: Health | None = None
    error: str | None = None


class SweepSummary(BaseModel):
    sweep_id: str
    path: str
    started_at: str | None = None
    system: str | None = None
    suite: str | None = None
    split: str | None = None
    ladder: str | None = None
    repeats: int | None = None
    task_count: int | None = None
    max_cost: float | None = None
    state: SweepState
    state_reason: str = ""
    rungs: list[RungState] = Field(default_factory=list)
    spend_usd: float | None = None
    unknown_cost_rungs: int = 0
    calibrated: bool = False
    routing: bool = False
    low_n_decisions: int = 0


class ItemRow(BaseModel):
    task_id: str
    strata: dict[str, str] = Field(default_factory=dict)
    units: int
    correct: int
    answered: int
    errors: int
    value: float | None = None  # the item's own primary-metric value, when the metric is per item
    mark: Literal["ok", "bad", "warn", "info"] = "info"


class Flip(BaseModel):
    task_id: str
    unit_id: str
    a_correct: float
    b_correct: float


class Guard(BaseModel):
    name: str
    ok: bool
    message: str
    blocking: bool = True


class RungPlan(BaseModel):
    name: str
    model: str | None
    params: dict[str, Any] = Field(default_factory=dict)
    rate: dict[str, float] | None = None
    priced: bool = False
    paid: bool = False  # priced above zero
    env: dict[str, bool] = Field(default_factory=dict)  # required variable -> set?
    no_heldout: bool = False
    concurrency: int | None = None


class Estimate(BaseModel):
    usd: float | None = None
    per_task: dict[str, float] = Field(default_factory=dict)
    source: str | None = None  # e.g. "measured in run <id>"
    missing: list[str] = Field(default_factory=list)  # rungs with no measured cost yet


class PlanRequest(BaseModel):
    """Everything a person or agent chooses before launching. The plan is derived from it."""

    kind: Literal["run", "sweep"]
    system: str
    suite: str = "m1"
    split: str = "public"
    task_types: list[str] = Field(default_factory=list)
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    ladder: str | None = None
    rungs: list[str] = Field(default_factory=list)
    limit: int | None = None  # run: first N tasks; sweep: stratified sample of N
    repeats: int = 1
    concurrency: int = 4
    max_cost: float | None = None
    stop_after_errors: int | None = 5
    ack_unpriced: bool = False


class Plan(BaseModel):
    plan_id: str
    request: PlanRequest
    system_name: str | None = None
    trusted: bool = False
    supports_model_param: bool = False
    reports_usage: bool = False
    task_types: list[str] = Field(default_factory=list)
    tasks: int = 0
    calls: int = 0
    rungs: list[RungPlan] = Field(default_factory=list)
    estimate: Estimate = Field(default_factory=Estimate)
    guards: list[Guard] = Field(default_factory=list)
    argv: list[str] = Field(default_factory=list)
    command: str = ""

    @property
    def ok(self) -> bool:
        return all(g.ok or not g.blocking for g in self.guards)


class Job(BaseModel):
    job_id: str
    kind: Literal["run", "sweep", "calibrate"]
    argv: list[str]
    launched_by: str = "ui"
    plan_id: str | None = None
    created_at: str
    pid: int | None = None
    state: Literal["running", "finished", "failed", "cancelled", "lost"] = "running"
    exit_code: int | None = None
    ended_at: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    sweep_id: str | None = None
    log_tail: list[str] = Field(default_factory=list)
