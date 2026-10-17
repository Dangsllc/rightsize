"""What agents get back: the MCP server's tools and the CLI's `--json` output both return these, so
the numbers match whichever way an agent asks.

The shapes follow from how agents read:
* Summary first, with handles. A payload leads with health and the headline numbers and points
  at the next call (`next`); detail comes from the call it names, not from a dump.
* Caveats travel with numbers. Every headline carries its interval, n, low/tiny-n flags, cost
  state, and whether it clears the trivial predictors; `caveats` holds sentences to quote as is.
* Bounded size. Documents are truncated unless asked for in full; lists have limits.
* A person can follow along. Each payload has a `ui_url` into the Workbench.
"""

from __future__ import annotations

import os
from typing import Any

from rightsize_core.calibration.sweep import MIN_ITEMS, decide
from rightsize_core.service import jobs, queries
from rightsize_core.service.index import Index
from rightsize_core.service.models import Headline, Plan, RunSummary, SweepSummary

UI_DEFAULT = "http://127.0.0.1:8765"
DOC_PREVIEW = 1500


def ui_url(path: str) -> str:
    """A Workbench link. Opening it needs the tokenized URL `rightsize ui` printed, once per session."""
    return os.environ.get("RIGHTSIZE_UI_URL", UI_DEFAULT).rstrip("/") + path


def _r(v: float | None, digits: int = 4) -> float | None:
    return None if v is None else round(v, digits)


def headline(h: Headline) -> dict[str, Any]:
    return {"task_type": h.task_type, "metric": h.metric, "value": _r(h.value), "ci95": [_r(h.lo), _r(h.hi)],
            "n_clusters": h.n_clusters, "low_n": h.low_n, "tiny_n": h.tiny_n,
            "floors": {k: _r(v) for k, v in h.floors.items()}, "clears_floors": h.clears_floors,
            "cost_per_task_usd": _r(h.cost_per_task, 6), "cost_state": h.cost_state, "error_rate": _r(h.error_rate)}


def run_brief(s: RunSummary) -> dict[str, Any]:
    return {"run_id": s.run_id, "system": s.system, "model": s.model, "params": s.params, "suite": s.suite,
            "split": s.split, "health": s.health, "health_reason": s.health_reason, "started_at": s.started_at,
            "sweep_id": s.sweep_id, "headlines": [headline(h) for h in s.headlines],
            "ui_url": ui_url(f"/runs/{s.run_id}")}


def list_runs(ix: Index, suite: str | None = None, split: str | None = None, system: str | None = None,
              health: str | None = None, limit: int = 20) -> dict[str, Any]:
    runs = [r for r in ix.runs() if (not suite or r.suite == suite) and (not split or r.split == split)
            and (not system or r.system == system) and (not health or r.health == health)]
    return {"total": len(runs), "runs": [run_brief(r) for r in runs[:limit]],
            "next": "summarize_run(run_id) for caveats, errors, tiers, and the worst items"}


def summarize_run(ix: Index, run_id: str) -> dict[str, Any]:
    d = queries.run_detail(ix, run_id)  # scores the run if nothing has yet
    s: RunSummary = d["summary"]
    tiers, worst = {}, {}
    for tt, r in d["task_types"].items():
        pm = r["primary_metric"]
        tiers[tt] = [{"tier": g["stratum"], "n": g["n"], "few": g["few"], pm: _r(g["metrics"].get(pm))} for g in r["grid"]]
        try:
            worst[tt] = [row.task_id for row in queries.items(ix, run_id, tt, show="wrong")[:5]]
        except Exception:  # noqa: BLE001 - an older run's items can't be re-read; the summary still stands
            worst[tt] = []
    out = run_brief(s)
    out.update(progress=s.progress.model_dump(), error_groups=[g.model_dump() for g in s.error_groups],
               caveats=s.caveats, notes=s.notes, tiers=tiers, worst_items=worst,
               next=[("get_item(run_id, task_id) explains one item: what was asked, how its label was built, "
                      "what the system answered"),
                     "list_items(run_id, task_type, tier) lists items in one tier"])
    return out


def list_items(ix: Index, run_id: str, task_type: str, tier: str | None = None, show: str = "wrong",
               limit: int = 25) -> dict[str, Any]:
    rows = queries.items(ix, run_id, task_type, tier, show)
    return {"total": len(rows), "items": [r.model_dump() for r in rows[:limit]],
            "ui_url": ui_url(f"/runs/{run_id}/items?task_type={task_type}"),
            "next": "get_item(run_id, task_id)"}


def get_item(ix: Index, run_id: str, task_id: str, repeat: int = 0, full_document: bool = False) -> dict[str, Any]:
    view = queries.item_view(ix, run_id, task_id, repeat).model_dump()
    for b in view["blocks"]:
        if b["kind"] != "document":
            continue
        text = b["text"]
        b["highlights"] = [{"label": h["label"], "mark": h["mark"], "text": text[h["start"]:h["end"]]}
                           for h in b["highlights"]]
        if not full_document and len(text) > DOC_PREVIEW:
            b["text"] = text[:DOC_PREVIEW]
            b["truncated"] = f"showing {DOC_PREVIEW} of {len(text)} characters; full_document=true for all"
        for key in ("fields", "columns", "rows", "row_marks", "notes"):
            b.pop(key, None)
    view["ui_url"] = ui_url(f"/runs/{run_id}/item?task={task_id}")
    return view


def diff_runs(ix: Index, run_a: str, run_b: str, flips_limit: int = 20) -> dict[str, Any]:
    c = queries.compare(ix, run_a, run_b)
    task_types = {}
    caveats = []
    for sec in c["sections"]:
        rows = {r["name"]: {"delta": _r(r["value"]), "ci95": [_r(r["lo"]), _r(r["hi"])], "flag": r["flag"],
                            "higher_is_better": r["higher_is_better"]} for r in sec["rows"]}
        flips = sec["flips"]
        task_types[sec["task_type"]] = {
            "primary_metric": sec["primary"], "primary": rows.get(sec["primary"]), "n_clusters": sec["n"],
            "flagged": {k: v for k, v in rows.items() if v["flag"]},
            "regressed": len(flips.get("regressed", [])), "improved": len(flips.get("improved", [])),
            "regressed_items": [f.task_id for f in flips.get("regressed", [])][:flips_limit],
            "improved_items": [f.task_id for f in flips.get("improved", [])][:flips_limit],
        }
        if sec["n"] < MIN_ITEMS:
            caveats.append(f"{sec['task_type']}: paired on {sec['n']} clusters (below {MIN_ITEMS}); treat the "
                           "deltas as indicative.")
    if c["diff"]["changes"]:
        caveats.append("More than the model changed between these runs (see `changed`); a difference may come "
                       "from any of it.")
    return {"a": run_brief(c["a"]), "b": run_brief(c["b"]), "changed": c["diff"]["changes"],
            "task_types": task_types, "caveats": caveats,
            "note": "Deltas are B minus A, paired on shared items; a flag needs the whole 95% interval past zero.",
            "ui_url": ui_url(f"/compare?a={run_a}&b={run_b}")}


def sweep_brief(w: SweepSummary) -> dict[str, Any]:
    return {"sweep_id": w.sweep_id, "state": w.state, "state_reason": w.state_reason, "system": w.system,
            "suite": w.suite, "split": w.split, "ladder": w.ladder,
            "rungs": [{"rung": r.rung, "model": r.model, "status": r.status, "run_id": r.run_id, "health": r.health}
                      for r in w.rungs],
            "spend_usd": _r(w.spend_usd, 4), "unknown_cost_rungs": w.unknown_cost_rungs,
            "calibrated": w.calibrated, "low_n_decisions": w.low_n_decisions,
            "ui_url": ui_url(f"/sweeps/{w.sweep_id}")}


def list_sweeps(ix: Index, limit: int = 20) -> dict[str, Any]:
    sweeps = ix.sweeps()
    kept = [w for w in sweeps if w.state != "empty"]
    return {"sweeps": [sweep_brief(w) for w in kept[:limit]], "empty_sweeps": len(sweeps) - len(kept),
            "next": "get_calibration(sweep_id) for the routes; preview_routing to try other thresholds"}


def _tau(cal: dict, d: dict, thresholds: dict[str, float]) -> float | None:
    th = thresholds
    return th.get(d["metric"], th.get(f"{d['task_type']}:{d['metric']}"))


def get_calibration(ix: Index, sweep_id: str) -> dict[str, Any]:
    w = ix.sweep(sweep_id)
    cal = queries.calibration(ix, sweep_id)
    out: dict[str, Any] = {"sweep": sweep_brief(w), "calibrated": cal is not None}
    if cal is None:
        out["next"] = "calibrate(sweep_id, thresholds) writes calibration.json and routing.yaml"
        return out
    routes, overrides, caveats = [], [], []
    for d in cal["decisions"]:
        ch = d["rungs"].get(d["chosen"]) if d["chosen"] else None
        row = {"task_type": d["task_type"], "tier": d["stratum"], "chosen": d["chosen"], "best": d["best"],
               "why": d["why"], "metric": d["metric"], "lower_bound": _r(ch["lo"]) if ch else None,
               "cost_per_task_usd": _r(ch["cost"], 6) if ch else None, "n_clusters": d["n_clusters"],
               "insufficient_n": d["insufficient_n"]}
        (routes if d["stratum"] == "all" else overrides).append(row)
        if d["stratum"] == "all" and d["insufficient_n"]:
            caveats.append(f"{d['task_type']}: the overall route rests on {d['n_clusters']} clusters (below "
                           f"{MIN_ITEMS}); don't present it as a finding.")
    if any(o["insufficient_n"] for o in overrides):
        caveats.append(f"Tiers with fewer than {MIN_ITEMS} clusters are shown but make no routing override.")
    routing = ix.sweep_dir(sweep_id) / "routing.yaml"
    out.update(thresholds=cal["thresholds"], delta=cal["delta"], floors=cal["floors"], routes=routes,
               tier_decisions=overrides, caveats=caveats,
               routing_yaml=routing.read_text() if routing.exists() else None,
               next="preview_routing(sweep_id, thresholds, delta) re-decides instantly; calibrate(...) saves")
    return out


def preview_routing(ix: Index, sweep_id: str, thresholds: dict[str, float] | None = None,
                    delta: float | None = None, floors: dict[str, float] | None = None) -> dict[str, Any]:
    """Re-decide every route from the saved bounds, as calibrate would, without writing anything."""
    cal = queries.calibration(ix, sweep_id)
    if cal is None:
        raise ValueError(f"Sweep {sweep_id} isn't calibrated yet; call calibrate first.")
    th = cal["thresholds"] if thresholds is None else thresholds
    dl = cal["delta"] if delta is None else delta
    fl = cal["floors"] if floors is None else floors
    rows = []
    for d in cal["decisions"]:
        chosen, why = decide(d["rungs"], d["best"], _tau(cal, d, th), dl, fl)
        rows.append({"task_type": d["task_type"], "tier": d["stratum"], "chosen": chosen, "why": why,
                     "changed": chosen != d["chosen"], "insufficient_n": d["insufficient_n"],
                     "cost_per_task_usd": _r(d["rungs"][chosen]["cost"], 6) if chosen else None})
    return {"thresholds": th, "delta": dl, "floors": fl, "decisions": rows,
            "note": "A preview from the saved bounds; calibrate(...) writes routing.yaml."}


def plan_payload(plan: Plan) -> dict[str, Any]:
    blocking = [f"{g.name}: {g.message}" for g in plan.guards if g.blocking and not g.ok]
    return {"plan_id": plan.plan_id, "ok": plan.ok, "kind": plan.request.kind, "blocking": blocking,
            "notes": [f"{g.name}: {g.message}" for g in plan.guards if g.ok or not g.blocking],
            "system": plan.system_name, "trusted": plan.trusted, "tasks": plan.tasks, "calls": plan.calls,
            "rungs": [{"name": r.name, "model": r.model, "price_per_mtok": r.rate, "paid": r.paid,
                       "missing_env": [k for k, ok in r.env.items() if not ok]} for r in plan.rungs],
            "estimate": {"usd": _r(plan.estimate.usd, 4), "source": plan.estimate.source,
                         "unmeasured_rungs": plan.estimate.missing},
            "max_cost": plan.request.max_cost, "command": plan.command,
            "next": ("Show this plan to the user; once they agree, start(plan_id)." if plan.ok
                     else "Fix what blocks it, then plan again.")}


def job_status(ix: Index, job_id: str) -> dict[str, Any]:
    j = jobs.load(ix.runs_dir, job_id)
    runs = []
    for rid in j.run_ids:
        try:
            s = ix.summary(rid)
        except LookupError:
            continue
        runs.append({"run_id": rid, "health": s.health, "health_reason": s.health_reason,
                     "progress": s.progress.model_dump(), "error_groups": [g.model_dump() for g in s.error_groups[:3]],
                     "headlines": [headline(h) for h in s.headlines], "ui_url": ui_url(f"/runs/{rid}")})
    cap = float(j.argv[j.argv.index("--max-cost") + 1]) if "--max-cost" in j.argv else None
    out = {"job_id": j.job_id, "kind": j.kind, "state": j.state, "launched_by": j.launched_by,
           "created_at": j.created_at, "exit_code": j.exit_code, "runs": runs,
           "sweep": sweep_brief(ix.sweep(j.sweep_id)) if j.sweep_id else None,
           "spend_usd": _r(sum(r["progress"]["spend_usd"] for r in runs), 4), "max_cost": cap,
           "log_tail": j.log_tail[-15:], "ui_url": ui_url(f"/jobs/{j.job_id}")}
    out["next"] = ("job_status(job_id) again in a minute; cancel_job(job_id) stops it" if j.state == "running"
                   else "summarize_run(run_id) or get_calibration(sweep_id)")
    return out


def conformance_payload(report) -> dict[str, Any]:
    return {"system": report.system, "ok": report.ok, "summary": report.summary,
            "findings": [{"task_id": f.task_id, "ok": f.ok, "message": f.message} for f in report.findings],
            "next": ("plan_run(system=...) to measure it" if report.ok
                     else "Fix the mapping (a failure here would be scored as a weak model), then check again.")}

