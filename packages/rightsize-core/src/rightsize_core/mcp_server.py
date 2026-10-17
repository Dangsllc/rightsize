"""`rightsize mcp`: the harness as MCP tools for agents (Claude Code and others), over stdio.

Every tool is a thin adapter over `rightsize_core.service.agent`, the same payloads `--json`
prints, so an agent and a person reading the Workbench see the same numbers. Spending is two-phase:
`plan_run` / `plan_sweep` return a plan_id with its guards and estimate, and `start(plan_id)`
rebuilds the plan and refuses if anything changed since. Jobs started here show in the Workbench
as launched by mcp, where a person can follow or cancel them.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from rightsize_core.service import agent

INSTRUCTIONS = """\
Rightsize benchmarks compliance AI systems with labels built by construction, and finds the cheapest
model that is good enough for each task. When you relay results:
- Quote a score with its 95% interval and n. Say "low n" or "too few items" when the payload says so;
  a routing decision on fewer than 30 clusters is not a finding.
- Errors and refusals are counted separately, never as wrong answers. A run with health no_signal
  measured nothing; report its error and hint, not a score.
- cost_state "unknown" means the system reported no usage. It is not $0. "zero" is a model priced at $0.
- Quote the `caveats` sentences rather than paraphrasing them away.
Before anything that calls models or spends money: plan_run or plan_sweep, show the user the plan
(calls, estimate, blocking guards), and call start(plan_id) only after the user agrees. Never ask
the user to paste an API key into chat; keys go in the checkout's .env. Held-out data only ever
goes to systems marked trusted, and the plan enforces it. A held-out run comes back as aggregates
only: its items and error text stay on this machine, in the Workbench.
"""

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
CALLS_SYSTEM = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
SPENDS = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
PROBES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


def build_server(root: Path, runs_dir: Path | None = None):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    from rightsize_core import __version__
    from rightsize_core.runner.run import default_runs_dir
    from rightsize_core.service import jobs
    from rightsize_core.service.env import load_dotenv
    from rightsize_core.service.index import Index, NotFound
    from rightsize_core.service.models import PlanRequest
    from rightsize_core.service.plans import build_plan

    load_dotenv(root)
    ix = Index(root, runs_dir or default_runs_dir(root))
    plans: dict[str, PlanRequest] = {}  # plan_id -> the request that made it
    mcp = MCPServer("rightsize", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")

    def tool(hints: dict, **kw):
        """Register a tool whose failures come back as one plain sentence the agent can act on."""
        def wrap(fn):
            @functools.wraps(fn)
            async def run(*args, **kwargs):
                try:
                    out = fn(*args, **kwargs)
                    return await out if hasattr(out, "__await__") else out
                except NotFound as e:
                    raise ToolError(f"Nothing called {e}. list_runs, list_sweeps, or job_status show what exists.") from e
                except (ValueError, KeyError, jobs.PlanBlocked) as e:
                    raise ToolError(str(e).strip("'\"")) from e
            return mcp.tool(annotations=ToolAnnotations(**hints), **kw)(run)
        return wrap

    # ------------------------------------------------------------------ reading results
    @tool(READ)
    def list_runs(suite: str | None = None, split: str | None = None, system: str | None = None,
                  health: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Runs, newest first, each with health and headline metrics (value, 95% interval, n, cost).
        health is one of ok, degraded, no_signal, aborted, interrupted, running, unscored."""
        return agent.list_runs(ix, suite, split, system, health, limit)

    @tool(READ)
    def summarize_run(run_id: str) -> dict[str, Any]:
        """One run: health and why, headlines against trivial predictors, error groups with what to check,
        caveats to quote, per-tier scores, and the worst items to look at next."""
        return agent.summarize_run(ix, run_id)

    @tool(READ)
    def list_items(run_id: str, task_type: str, tier: str | None = None, show: str = "wrong",
                   limit: int = 25) -> dict[str, Any]:
        """Items of one task type, worst first. tier is "key=value" as summarize_run lists it;
        show is all, wrong, or errors."""
        return agent.list_items(ix, run_id, task_type, tier, show, limit)

    @tool(READ)
    def get_item(run_id: str, task_id: str, repeat: int = 0, full_document: bool = False) -> dict[str, Any]:
        """One item: what was asked, how its expected answer was built, and what the system answered,
        with quoted evidence. Documents are cut to 1,500 characters unless full_document is true."""
        return agent.get_item(ix, run_id, task_id, repeat, full_document)

    @tool(READ)
    def diff_runs(run_a: str, run_b: str) -> dict[str, Any]:
        """Paired comparison, B minus A: what changed between the runs, each metric's delta with its 95%
        interval and regression/improved flag, and the items that flipped."""
        return agent.diff_runs(ix, run_a, run_b)

    @tool(READ)
    def list_sweeps(limit: int = 20) -> dict[str, Any]:
        """Sweeps (one suite across a ladder of models), newest first: state, rungs, spend, calibration."""
        return agent.list_sweeps(ix, limit)

    @tool(READ)
    def get_calibration(sweep_id: str) -> dict[str, Any]:
        """The cheapest good-enough rung per task type and tier, why, its lower bound and cost, and the
        routing.yaml calibrate wrote."""
        return agent.get_calibration(ix, sweep_id)

    @tool(READ)
    def preview_routing(sweep_id: str, thresholds: dict[str, float] | None = None, delta: float | None = None,
                        floors: dict[str, float] | None = None) -> dict[str, Any]:
        """Re-decide a calibrated sweep's routes for other thresholds (metric name -> value, e.g.
        {"ndcg@10": 0.8}), margin, or floors, instantly and without writing anything."""
        return agent.preview_routing(ix, sweep_id, thresholds, delta, floors)

    @tool(READ)
    def list_jobs(limit: int = 10) -> dict[str, Any]:
        """Background runs, sweeps, and calibrations, from this server or the Workbench, newest first."""
        return {"jobs": [{"job_id": j.job_id, "kind": j.kind, "state": j.state, "launched_by": j.launched_by,
                          "created_at": j.created_at} for j in jobs.list_jobs(ix.runs_dir)[:limit]]}

    @tool(READ)
    def job_status(job_id: str) -> dict[str, Any]:
        """A job's state, its runs' progress, health, and errors so far, spend against the cap, and output."""
        return agent.job_status(ix, job_id)

    # ------------------------------------------------------------------ systems
    @tool(PROBES)
    async def check_conformance(system: str, suite: str = "m1") -> dict[str, Any]:
        """Send two public items per task type to a system and check its answers map onto the task
        contracts. Run this before measuring a new system: a mapping bug looks exactly like a weak model
        in the scores. These are real calls; on a paid system they cost a little, so confirm first."""
        from rightsize_core.conformance.check import run_conformance
        from rightsize_core.systems.loader import load_system

        sysobj = load_system(system, root, suite)
        try:
            await sysobj.preflight()
            return agent.conformance_payload(await run_conformance(root, sysobj, suite))
        finally:
            await sysobj.aclose()

    # ------------------------------------------------------------------ planning and starting
    async def plan(req: PlanRequest) -> dict[str, Any]:
        p = await build_plan(root, req, ix)
        plans[p.plan_id] = req
        return agent.plan_payload(p)

    @tool(CALLS_SYSTEM)
    async def plan_run(system: str, suite: str = "m1", split: str = "public", task_types: list[str] | None = None,
                       model: str | None = None, params: dict[str, Any] | None = None, limit: int | None = None,
                       repeats: int = 1, concurrency: int = 4, max_cost: float | None = None,
                       stop_after_errors: int | None = 5, ack_unpriced: bool = False) -> dict[str, Any]:
        """Plan one run without starting it: calls, measured-cost estimate, and every guard (held-out data,
        spend cap required for priced models, keys present, unpriced models). Starts nothing.
        system: ref:bm25, ref:llm, a config path under configs/systems/, a rightsize/v1 URL, or mcp+..."""
        return await plan(PlanRequest(kind="run", system=system, suite=suite, split=split,
                                      task_types=task_types or [], model=model, params=params or {}, limit=limit,
                                      repeats=repeats, concurrency=concurrency, max_cost=max_cost,
                                      stop_after_errors=stop_after_errors, ack_unpriced=ack_unpriced))

    @tool(CALLS_SYSTEM)
    async def plan_sweep(system: str, ladder: str = "configs/ladders/default.yaml", rungs: list[str] | None = None,
                         suite: str = "m1", split: str = "dev", task_types: list[str] | None = None,
                         sample: int | None = None, repeats: int = 1, concurrency: int = 4,
                         max_cost: float | None = None, stop_after_errors: int | None = 5,
                         ack_unpriced: bool = False) -> dict[str, Any]:
        """Plan a sweep (the same tasks across a ladder of models) without starting it. For a first look
        at cost, plan a pilot: sample=20, repeats=1, max_cost=5. Starts nothing."""
        return await plan(PlanRequest(kind="sweep", system=system, ladder=ladder, rungs=rungs or [], suite=suite,
                                      split=split, task_types=task_types or [], limit=sample, repeats=repeats,
                                      concurrency=concurrency, max_cost=max_cost,
                                      stop_after_errors=stop_after_errors, ack_unpriced=ack_unpriced))

    @tool(SPENDS)
    async def start(plan_id: str) -> dict[str, Any]:
        """Start a planned run or sweep in the background. This calls models and can spend money: only
        after the user has seen the plan and agreed. Refuses if the plan changed since it was made."""
        req = plans.get(plan_id)
        if req is None:
            raise ValueError(f"No plan {plan_id} from this session; call plan_run or plan_sweep first.")
        p = await build_plan(root, req, ix)
        if p.plan_id != plan_id:
            plans[p.plan_id] = req
            return {"started": False, "reason": "The plan changed since it was made (a key, a price, a measured "
                                                "cost, or a guard). Show the user the new plan.",
                    "plan": agent.plan_payload(p)}
        if not p.ok:
            return {"started": False, "reason": "The plan doesn't pass its guards.", "plan": agent.plan_payload(p)}
        job = jobs.start_plan(root, ix.runs_dir, p, launched_by="mcp")
        return {"started": True, "job_id": job.job_id, "ui_url": agent.ui_url(f"/jobs/{job.job_id}"),
                "next": "job_status(job_id)"}

    @tool(SPENDS)
    def calibrate(sweep_id: str, thresholds: dict[str, float] | None = None, delta: float = 0.03,
                  floors: dict[str, float] | None = None, emit: bool = True) -> dict[str, Any]:
        """Pick the cheapest good-enough rung per task type and tier and write calibration.json (and
        routing.yaml unless emit is false), as a background job. Calls no models. thresholds maps a metric
        to its bar, e.g. {"group.macro_f1": 0.7, "ndcg@10": 0.6}; try them first with preview_routing."""
        sdir = ix.sweep_dir(sweep_id)
        argv = ["calibrate", str(sdir), "--delta", f"{delta:g}"]
        argv += [a for m, v in (thresholds or {}).items() for a in ("--threshold", f"{m}={v:g}")]
        argv += [a for m, v in (floors or {}).items() for a in ("--floor", f"{m}={v:g}")]
        if emit:
            argv += ["--emit", str(sdir / "routing.yaml")]
        job = jobs.start(root, ix.runs_dir, argv, "calibrate", "mcp")
        return {"job_id": job.job_id, "ui_url": agent.ui_url(f"/jobs/{job.job_id}"),
                "next": "job_status(job_id), then get_calibration(sweep_id)"}

    @tool({"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Stop a running job. Calls in flight finish; each run records `aborted: cancelled`."""
        j = jobs.cancel(ix.runs_dir, job_id)
        return {"job_id": j.job_id, "state": j.state, "next": "job_status(job_id) until the state settles"}

    return mcp


def serve(root: Path, runs_dir: Path | None = None) -> None:
    build_server(root, runs_dir).run("stdio")
