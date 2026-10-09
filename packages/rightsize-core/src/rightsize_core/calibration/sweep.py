"""Model right-sizing: run one suite across a ladder of models, then pick, per task type and
difficulty tier, the cheapest rung that is good enough.

"Good enough" is either an absolute bar (the metric's lower 95% bound clears the threshold) or
non-inferiority (the paired lower bound of rung minus best is above -delta). A rung must also keep
its error rate under 2% and, when a floor metric is given (e.g. deficiency recall), clear it.
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
from rightsize_core.scoring.stats import bootstrap, paired_difference

MAX_ERROR = 0.02
MIN_ITEMS = 30


@dataclass
class Rung:
    name: str
    model: str
    params: dict


def load_ladder(path: Path) -> list[Rung]:
    data = yaml.safe_load(path.read_text())
    return [Rung(r["name"], r["model"], r.get("params", {})) for r in data["rungs"]]


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


def _filter(units, key: str | None, val: str | None):
    return units if key is None else [u for u in units if u.strata.get(key) == val]


def calibrate(root: Path, sweep_dir: Path, thresholds: dict[str, float], delta: float,
              floors: dict[str, float] | None = None, n_resamples: int = 1000) -> dict[str, Any]:
    sweep = json.loads((sweep_dir / "sweep.json").read_text())
    runs = []
    for idx, r in enumerate(sweep["runs"]):
        run_dir = Path(r["run_dir"])
        manifest, by_type, rows = unit_records(root, run_dir)
        cost = [x["cost_usd"] for x in rows]
        runs.append({
            "rung": r["rung"], "model": r["model"], "params": r["params"], "by_type": by_type,
            "cost_per_task": (sum(cost) / len(cost)) if cost and all(c is not None for c in cost) else None,
            "suite": manifest.dataset.suite, "ladder_index": idx,
        })
    pack = get_pack(load_suite_config(root, runs[0]["suite"])["pack"])
    result: dict[str, Any] = {"sweep_id": sweep["sweep_id"], "thresholds": thresholds, "delta": delta,
                              "floors": floors or {}, "decisions": [], "points": []}
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
                floor_ok = all(b[m].value >= v for m, v in (floors or {}).items() if m in b)
                est[r["rung"]] = {"ladder_index": r["ladder_index"],
                                  "value": b[metric].value, "lo": b[metric].lo, "hi": b[metric].hi,
                                  "n": b[metric].n_clusters, "error_rate": err, "floor_ok": floor_ok,
                                  "cost": r["cost_per_task"], "units": units}
                if key is None:
                    result["points"].append({"task_type": tt, "rung": r["rung"], "metric": metric,
                                             "value": b[metric].value, "lo": b[metric].lo, "hi": b[metric].hi,
                                             "cost_per_task": r["cost_per_task"]})
            if not est:
                continue
            best = max(est, key=lambda k: est[k]["value"])
            # Cheapest first by reported cost; if any rung's cost is unknown, use the ladder's own
            # order (the ladder file lists rungs cheapest first) for all of them.
            if all(est[k]["cost"] is not None for k in est):
                order, ordering = sorted(est, key=lambda k: (est[k]["cost"], est[k]["ladder_index"])), "reported cost"
            else:
                order, ordering = sorted(est, key=lambda k: est[k]["ladder_index"]), "ladder order (cost unknown)"
            chosen, why = None, None
            for name in order:
                e = est[name]
                if e["error_rate"] > MAX_ERROR or not e["floor_ok"]:
                    continue
                if tau is not None and e["lo"] >= tau:
                    chosen, why = name, f"lower bound {e['lo']:.3f} >= {tau}"
                    break
                if name == best:
                    chosen, why = name, "best rung"
                    break
                d = paired_difference(est[best]["units"], e["units"], spec.aggregate, n_resamples).get(metric)
                if d is not None and d.lo >= -delta:
                    chosen, why = name, f"within {delta} of best ({best}): diff lower bound {d.lo:+.3f}"
                    break
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
            "cost_per_task_usd": d["rungs"][d["chosen"]]["cost"], "why": d["why"]}}
        if d["stratum"] == "all":
            routes[key] = entry
        elif not d["insufficient_n"] and d["chosen"] != result_route(result, d["task_type"]):
            overrides.setdefault(key, {})[d["stratum"]] = {"model": rung.model, "params": rung.params}
    doc = {"generated_by": "rightsize calibrate", "sweep_id": result["sweep_id"], "thresholds": result["thresholds"],
           "delta": result["delta"], "routes": routes, "stratum_overrides": overrides}
    return yaml.safe_dump(doc, sort_keys=False)


def result_route(result: dict, task_type: str) -> str | None:
    for d in result["decisions"]:
        if d["task_type"] == task_type and d["stratum"] == "all":
            return d["chosen"]
    return None
