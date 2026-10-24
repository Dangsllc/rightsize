"""Model right-sizing: run one suite across a ladder of models, then pick, per task type and
difficulty tier, the cheapest rung that is good enough.

"Good enough" is either an absolute bar (the metric's lower 95% bound clears the threshold) or
non-inferiority (the paired lower bound of rung minus best is above -delta). A rung must also keep
its error rate under 2% and, when a floor metric is given (e.g. deficiency recall), clear it.
A rung whose run failed (every call errored) is never calibrated on; it is listed in `skipped_rungs`.

`calibration.json` keeps every input the rule needs (each rung's bounds, its paired difference
from the best rung, and its metric values), so a viewer can re-decide for other thresholds,
margins, and floors without resampling. `decide` is the rule; anything that re-decides mirrors it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rightsize_core.integrity.dataset import load_suite_config
from rightsize_core.packs import get_pack
from rightsize_core.runner.score import unit_records
from rightsize_core.schema import is_failed
from rightsize_core.scoring.stats import bootstrap, paired_difference

MAX_ERROR = 0.02
MIN_ITEMS = 30


@dataclass
class Rung:
    name: str
    model: str
    params: dict
    concurrency: int | None = None  # overrides the sweep's --concurrency (local servers want 1)


def load_ladder(path: Path) -> list[Rung]:
    data = yaml.safe_load(path.read_text())
    return [Rung(r["name"], r["model"], r.get("params", {}), r.get("concurrency")) for r in data["rungs"]]


def sample_tasks(tasks: list, n: int | None, seed: int = 7) -> list:
    """Stratified, deterministic sample: round-robin over (task_type, strata) buckets."""
    if not n or n >= len(tasks):
        return tasks
    from collections import defaultdict

    from rightsize_core.scoring.stats import np

    buckets: dict[tuple, list] = defaultdict(list)
    for t in tasks:
        buckets[(t.task_type, tuple(sorted(t.strata.items())))].append(t)
    rng = np.random.default_rng(seed)
    for b in buckets.values():
        rng.shuffle(b)
    out, keys = [], sorted(buckets)
    while len(out) < n and any(buckets[k] for k in keys):
        for k in keys:
            if buckets[k] and len(out) < n:
                out.append(buckets[k].pop())
    return out


def resolve_run_dir(root: Path, run_dir: str) -> Path:
    """sweep.json stores run dirs relative to the root; older sweeps stored absolute paths, which
    break once the checkout moves, so fall back to the same name under this root's runs/."""
    path = Path(run_dir)
    if not path.is_absolute():
        return root / path
    if not path.exists() and (root / "runs" / path.name).exists():
        return root / "runs" / path.name
    return path


def _filter(units, key: str | None, val: str | None):
    return units if key is None else [u for u in units if u.strata.get(key) == val]


def rung_order(rungs: dict[str, dict]) -> tuple[list[str], str]:
    """Cheapest first by reported cost; if any rung's cost is unknown, use the ladder's own order
    (the ladder file lists rungs cheapest first) for all of them."""
    if all(rungs[k]["cost"] is not None for k in rungs):
        return sorted(rungs, key=lambda k: (rungs[k]["cost"], rungs[k]["ladder_index"])), "reported cost"
    return sorted(rungs, key=lambda k: rungs[k]["ladder_index"]), "ladder order (cost unknown)"


def floor_ok(rung: dict, floors: dict[str, float] | None) -> bool:
    return all(rung["metrics"][m] >= v for m, v in (floors or {}).items() if m in rung["metrics"])


def decide(rungs: dict[str, dict], best: str, tau: float | None, delta: float,
           floors: dict[str, float] | None = None) -> tuple[str | None, str | None]:
    """The cheapest rung that is good enough, and why. `rungs` holds calibration.json's per-rung
    entries for one task type and tier."""
    for name in rung_order(rungs)[0]:
        e = rungs[name]
        if e["error_rate"] > MAX_ERROR or not floor_ok(e, floors):
            continue
        if tau is not None and e["lo"] >= tau:
            return name, f"lower bound {e['lo']:.3f} >= {tau}"
        if name == best:
            return name, "best rung"
        if e["diff_lo"] is not None and e["diff_lo"] >= -delta:
            return name, f"within {delta} of best ({best}): diff lower bound {e['diff_lo']:+.3f}"
    return None, None


def calibrate(root: Path, sweep_dir: Path, thresholds: dict[str, float], delta: float,
              floors: dict[str, float] | None = None, n_resamples: int = 1000) -> dict[str, Any]:
    sweep = json.loads((sweep_dir / "sweep.json").read_text())
    runs, skipped = [], []
    for idx, r in enumerate(sweep["runs"]):
        run_dir = resolve_run_dir(root, r["run_dir"])
        manifest, by_type, rows = unit_records(root, run_dir)
        if is_failed(manifest.status):  # every call errored: it measured nothing
            skipped.append({"rung": r["rung"], "why": manifest.status})
            continue
        cost = [x["cost_usd"] for x in rows]
        runs.append({
            "rung": r["rung"], "model": r["model"], "params": r["params"], "by_type": by_type,
            "cost_per_task": (sum(cost) / len(cost)) if cost and all(c is not None for c in cost) else None,
            "suite": manifest.dataset.suite, "ladder_index": idx,
        })
    skipped += [{"rung": r["rung"], "why": r.get("error") or "failed"}
                for r in sweep.get("rungs") or [] if r.get("status") == "failed"
                and r["rung"] not in {x["rung"] for x in skipped}]
    if not runs:
        why = "; ".join(f"{x['rung']}: {x['why']}" for x in skipped)
        raise ValueError(f"Nothing to calibrate in sweep {sweep['sweep_id']}: no rung finished with answers"
                         + (f" ({why})" if why else ""))
    pack = get_pack(load_suite_config(root, runs[0]["suite"])["pack"])
    result: dict[str, Any] = {"sweep_id": sweep["sweep_id"], "thresholds": thresholds, "delta": delta,
                              "floors": floors or {}, "decisions": [], "points": [], "skipped_rungs": skipped}
    task_types = sorted(set.intersection(*(set(r["by_type"]) for r in runs)))
    for tt in task_types:
        spec = pack.task_types[tt]
        # A threshold may name any metric this task type reports, optionally prefixed "task_type:".
        produced = set(spec.aggregate(runs[0]["by_type"][tt]))
        named = [m.split(":", 1)[-1] for m in thresholds
                 if (":" not in m or m.startswith(tt + ":")) and m.split(":", 1)[-1] in produced]
        metric = named[0] if named else spec.primary_metric
        tau = thresholds.get(metric, thresholds.get(f"{tt}:{metric}"))
        strata_keys = sorted({k for r in runs for u in r["by_type"][tt] for k in u.strata})
        slices: list[tuple[str | None, str | None]] = [(None, None)]
        for k in strata_keys:
            vals = sorted({u.strata[k] for r in runs for u in r["by_type"][tt] if k in u.strata})
            slices += [(k, v) for v in vals]
        for key, val in slices:
            est = {}
            for r in runs:
                units = _filter(r["by_type"][tt], key, val)
                if not units:
                    continue
                b = bootstrap(units, spec.aggregate, n_resamples)
                if metric not in b:
                    continue
                err = max((b[m].value for m in b if m.endswith("error_rate")), default=0.0)
                est[r["rung"]] = {"ladder_index": r["ladder_index"],
                                  "value": b[metric].value, "lo": b[metric].lo, "hi": b[metric].hi,
                                  "n": b[metric].n_clusters, "error_rate": err,
                                  "metrics": {m: v.value for m, v in b.items()},
                                  "cost": r["cost_per_task"], "units": units}
                est[r["rung"]]["floor_ok"] = floor_ok(est[r["rung"]], floors)
                if key is None:
                    result["points"].append({"task_type": tt, "rung": r["rung"], "metric": metric,
                                             "value": b[metric].value, "lo": b[metric].lo, "hi": b[metric].hi,
                                             "cost_per_task": r["cost_per_task"]})
            if not est:
                continue
            best = max(est, key=lambda k: est[k]["value"])
            # Every rung's paired difference from the best, not only the ones this decision
            # reaches, so other thresholds, margins, and floors can be tried from the JSON alone.
            for name, e in est.items():
                d = None if name == best else paired_difference(
                    est[best]["units"], e["units"], spec.aggregate, n_resamples).get(metric)
                e["diff_lo"] = 0.0 if name == best else (None if d is None else d.lo)
            chosen, why = decide(est, best, tau, delta, floors)
            ordering = rung_order(est)[1]
            n = est[best]["n"]
            result["decisions"].append({
                "task_type": tt, "stratum": f"{key}={val}" if key else "all", "metric": metric,
                "chosen": chosen, "why": why or "no rung qualifies", "best": best, "ordering": ordering,
                "insufficient_n": n < MIN_ITEMS, "n_clusters": n,
                "rungs": {k: {kk: vv for kk, vv in v.items() if kk != "units"} for k, v in est.items()},
            })
    return result


def routing_yaml(result: dict, ladder: dict[str, Rung], key_map: dict[str, str] | None = None) -> str:
    routes, overrides = {}, {}
    for d in result["decisions"]:
        if not d["chosen"]:
            continue
        rung = ladder[d["chosen"]]
        key = (key_map or {}).get(d["task_type"], d["task_type"])
        entry = {"model": rung.model, "params": rung.params, "evidence": {
            "metric": d["metric"], "lower_bound": round(d["rungs"][d["chosen"]]["lo"], 3),
            "cost_per_task_usd": _round(d["rungs"][d["chosen"]]["cost"]), "why": d["why"],
            "n_clusters": d["n_clusters"], "insufficient_n": d["insufficient_n"]}}
        if d["stratum"] == "all":
            routes[key] = entry
        elif not d["insufficient_n"] and d["chosen"] != result_route(result, d["task_type"]):
            overrides.setdefault(key, {})[d["stratum"]] = {"model": rung.model, "params": rung.params}
    doc = {"generated_by": "rightsize calibrate", "sweep_id": result["sweep_id"], "thresholds": result["thresholds"],
           "delta": result["delta"], "routes": routes, "stratum_overrides": overrides}
    return yaml.safe_dump(doc, sort_keys=False)


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 8)


def result_route(result: dict, task_type: str) -> str | None:
    for d in result["decisions"]:
        if d["task_type"] == task_type and d["stratum"] == "all":
            return d["chosen"]
    return None
