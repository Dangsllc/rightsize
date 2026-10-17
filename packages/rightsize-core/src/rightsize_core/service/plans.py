"""Before anything spends money or touches held-out data: what exactly will run, what it may cost,
and which guards it passes. The CLI's `sweep --dry-run`, the Workbench's launch form, and the
MCP `plan_*` tools all build the same Plan; a launch must present the plan_id it was shown, so a
plan that changed since (a key removed, a price edited, a pilot finished) is reviewed again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from collections import Counter
from pathlib import Path

from rightsize_core.service.env import env_present
from rightsize_core.service.models import Estimate, Guard, Plan, PlanRequest, RungPlan

DEFAULT_PRICING = "configs/pricing/2026-10.yaml"
INFO_TIMEOUT_S = 20.0


def _argv(req: PlanRequest, pricing: str) -> list[str]:
    a = [req.kind, "--system", req.system, "--suite", req.suite, "--split", req.split]
    for tt in req.task_types:
        a += ["--task-type", tt]
    if req.kind == "run":
        if req.model:
            a += ["--model", req.model]
        for k, v in sorted(req.params.items()):
            a += ["--param", f"{k}={json.dumps(v)}"]
        if req.limit:
            a += ["--limit", str(req.limit)]
    else:
        a += ["--ladder", req.ladder or "configs/ladders/default.yaml"]
        if req.rungs:
            a += ["--rungs", ",".join(req.rungs)]
        if req.limit:
            a += ["--sample", str(req.limit)]
    a += ["--repeats", str(req.repeats), "--concurrency", str(req.concurrency), "--pricing", pricing]
    if req.max_cost is not None:
        a += ["--max-cost", f"{req.max_cost:g}"]
    if req.stop_after_errors:
        a += ["--stop-after-errors", str(req.stop_after_errors)]
    return a


def _measured(index, suite: str, system: str | None, model: str | None, params: dict) -> tuple[dict, str | None]:
    """Cost per task by task type from the newest finished run of the same system, model, and
    params on this suite. Measured numbers only: there is no token model behind an estimate."""
    if index is None or not model:
        return {}, None
    for s in index.runs():
        if (s.suite, s.system, s.model) != (suite, system, model) or s.params != params:
            continue
        per = {h.task_type: h.cost_per_task for h in s.headlines if h.cost_per_task is not None}
        if per:
            return per, s.run_id
    return {}, None


async def build_plan(root: Path, req: PlanRequest, index=None, pricing_file: str = DEFAULT_PRICING) -> Plan:
    from rightsize_core.calibration.sweep import load_ladder, sample_tasks
    from rightsize_core.integrity.dataset import check_suite, load_tasks
    from rightsize_core.systems.loader import load_system
    from rightsize_core.usage.pricing import Pricing

    guards: list[Guard] = []
    pricing = Pricing(root / pricing_file) if (root / pricing_file).exists() else None

    # ---- what would run
    tasks = []
    try:
        tasks = load_tasks(root, req.suite, req.split, req.task_types or None)
    except (FileNotFoundError, KeyError, OSError) as e:
        guards.append(Guard(name="tasks", ok=False, message=f"No {req.split} tasks for suite {req.suite}: {e}"))
    if tasks and req.split != "heldout":
        problems = check_suite(root, req.suite)
        guards.append(Guard(name="data check", ok=not problems,
                            message="Suite data matches its manifest." if not problems
                            else f"`rightsize data check --suite {req.suite}` fails: {problems[0]}"))
    if req.kind == "run" and req.limit:
        tasks = tasks[: req.limit]
    elif req.kind == "sweep":
        tasks = sample_tasks(tasks, req.limit)

    rung_specs: list[RungPlan] = []
    if req.kind == "sweep":
        try:
            ladder = load_ladder(root / (req.ladder or "configs/ladders/default.yaml"))
            if req.rungs:
                ladder = [r for r in ladder if r.name in set(req.rungs)]
            rung_specs = [RungPlan(name=r.name, model=r.model, params=r.params, concurrency=r.concurrency)
                          for r in ladder]
            if not rung_specs:
                guards.append(Guard(name="ladder", ok=False, message="No rungs selected."))
        except (OSError, KeyError, TypeError) as e:
            guards.append(Guard(name="ladder", ok=False, message=f"Can't read the ladder: {e}"))
    else:
        rung_specs = [RungPlan(name="run", model=req.model, params=req.params)]

    # ---- the system
    info, trusted, name = None, False, None
    try:
        sysobj = load_system(req.system, root, req.suite)
    except Exception as e:  # noqa: BLE001
        sysobj = None
        guards.append(Guard(name="system", ok=False, message=f"Can't load the system: {e}"))
    if sysobj is not None:
        try:
            first = rung_specs[0] if rung_specs else RungPlan(name="run", model=req.model, params=req.params)
            sysobj.configure(first.model, first.params)
            await asyncio.wait_for(sysobj.preflight(), INFO_TIMEOUT_S)
            info = await asyncio.wait_for(sysobj.info(), INFO_TIMEOUT_S)
            trusted, name = sysobj.trusted, info.name
            for r in rung_specs:
                sysobj.configure(r.model, r.params)
                r.env = {v: env_present(v) for v in sysobj.required_env()}
            guards.append(Guard(name="system", ok=True, message=f"{info.name} answered and passed its preflight."))
        except Exception as e:  # noqa: BLE001
            guards.append(Guard(name="system", ok=False, message=f"The system didn't pass its preflight: {e!r}"[:400]))
        finally:
            try:
                await sysobj.aclose()
            except Exception:  # noqa: BLE001, S110  (closing must never hide the preflight result)
                pass

    supported = tasks
    if info is not None:
        supported = [t for t in tasks if t.task_type in info.task_types]
        skipped = sorted({t.task_type for t in tasks} - set(info.task_types))
        if tasks and not supported:
            guards.append(Guard(name="task types", ok=False,
                                message=f"{info.name} accepts none of these task types ({', '.join(skipped)})."))
        elif skipped:
            guards.append(Guard(name="task types", ok=False, blocking=False,
                                message=f"{info.name} doesn't accept {', '.join(skipped)}; those tasks are skipped."))
        if req.kind == "sweep" and not info.supports_model_param:
            guards.append(Guard(name="sweepable", ok=False,
                                message=f"{info.name} doesn't take a model parameter, so it can't be swept."))

    # ---- held-out data never leaves trusted hands
    if req.split == "heldout":
        if sysobj is not None and not trusted:
            guards.append(Guard(name="held-out", ok=False, message=(
                "Held-out tasks only go to systems marked `trusted: true`. Held-out data sent to a third "
                "party is no longer held out.")))
        blocked = [r.name for r in rung_specs if r.params.get("no_heldout")]
        if blocked:
            guards.append(Guard(name="held-out", ok=False, message=(
                f"{', '.join(blocked)} {'is' if len(blocked) == 1 else 'are'} marked no_heldout "
                "(e.g. a provider that trains on prompts).")))

    # ---- keys
    for r in rung_specs:
        missing = [v for v, ok in r.env.items() if not ok]
        if missing:
            label = "" if req.kind == "run" else f"{r.name}: "
            guards.append(Guard(name="keys", ok=False, message=(
                f"{label}{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. Add "
                f"{'it' if len(missing) == 1 else 'them'} to .env (the value is never shown here).")))

    # ---- money
    unpriced = []
    for r in rung_specs:
        r.rate = pricing.rate(r.model) if pricing and r.model else None
        r.priced = r.rate is not None
        r.paid = bool(r.rate) and any(v > 0 for v in r.rate.values())
        r.no_heldout = bool(r.params.get("no_heldout"))
        if r.model and not r.priced:
            unpriced.append(r.name if req.kind == "sweep" else r.model)
    paid = any(r.paid for r in rung_specs)
    reports_usage = bool(info and info.reports_usage)
    if paid and req.max_cost is None:
        guards.append(Guard(name="spend cap", ok=False,
                            message="Set a spend cap: at least one model here is priced above $0."))
    if unpriced or (paid and not reports_usage and info is not None):
        why = (f"no price on file for {', '.join(unpriced)}" if unpriced
               else f"{name} doesn't report token usage")
        guards.append(Guard(name="unknown cost", ok=req.ack_unpriced, message=(
            f"Cost will be unknown ({why}), so the spend cap can't stop it. Confirm to run anyway.")))
    if req.max_cost is not None:
        conc = max([r.concurrency or req.concurrency for r in rung_specs] or [req.concurrency])
        guards.append(Guard(name="spend cap", ok=True, blocking=False, message=(
            f"The cap stops new calls once reported spend passes ${req.max_cost:g}; up to {conc} calls "
            "already in flight may finish.")))

    # ---- estimate from measured cost only
    per_type = Counter(t.task_type for t in supported)
    est = Estimate()
    total, sources = 0.0, set()
    for r in rung_specs:
        per, src = _measured(index, req.suite, name, r.model, r.params)
        if r.priced and not r.paid:
            per = {tt: 0.0 for tt in per_type}
            src = src or "priced at $0"
        if not per or any(tt not in per for tt in per_type):
            est.missing.append(r.name)
            continue
        cost = sum(per[tt] * n * req.repeats for tt, n in per_type.items())
        total += cost
        est.per_task.update({f"{r.name}:{tt}" if req.kind == "sweep" else tt: v for tt, v in per.items()})
        sources.add(src)
    if rung_specs and not est.missing:
        est.usd = total
    measured = sorted(s for s in sources if s and s != "priced at $0")
    est.source = (f"measured in {', '.join(measured)}" if measured else
                  "priced at $0" if sources else None)

    pricing_rel = pricing_file
    argv = _argv(req, pricing_rel)
    plan = Plan(
        plan_id="", request=req, system_name=name, trusted=trusted,
        supports_model_param=bool(info and info.supports_model_param), reports_usage=reports_usage,
        task_types=sorted(per_type), tasks=len(supported), calls=len(supported) * req.repeats * max(1, len(rung_specs)),
        rungs=rung_specs, estimate=est, guards=guards, argv=argv, command="uv run rightsize " + shlex.join(argv),
    )
    plan.plan_id = plan_hash(plan, pricing.sha256 if pricing else None)
    return plan


def plan_hash(plan: Plan, pricing_sha: str | None) -> str:
    body = plan.model_dump(mode="json", exclude={"plan_id"})
    body["pricing_sha256"] = pricing_sha
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
