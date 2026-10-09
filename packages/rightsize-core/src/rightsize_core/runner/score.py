"""Score a finished run: unit records, bootstrap CIs, floors, stability, cost, verdict."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from rightsize_core.integrity.dataset import load_tasks
from rightsize_core.packs import UnitRecord, get_pack
from rightsize_core.runner.run import load_results, outcome_kind
from rightsize_core.schema import Outcome, RunManifest
from rightsize_core.scoring.stats import bootstrap, repeat_stability

DEGRADED_THRESHOLD = 0.02


def unit_records(root: Path, run_dir: Path) -> tuple[RunManifest, dict[str, list[UnitRecord]], list[dict]]:
    manifest = RunManifest.model_validate_json((run_dir / "manifest.json").read_text())
    tasks = {t.id: t for t in load_tasks(root, manifest.dataset.suite, manifest.dataset.split)}
    rows = load_results(run_dir)
    by_type: dict[str, list[UnitRecord]] = defaultdict(list)
    packs: dict[str, Any] = {}
    for row in rows:
        task = tasks.get(row["task_id"])
        if task is None:
            continue
        pack = packs.setdefault(task.pack, get_pack(task.pack))
        spec = pack.task_types[task.task_type]
        outcome = Outcome.model_validate(row["outcome"])
        kind = outcome_kind(outcome)
        pred = spec.prediction_model.model_validate(outcome.prediction) if kind == "predicted" else None
        for u in spec.score_item(task, pred, kind):
            u.extra["repeat"] = row["repeat"]
            by_type[task.task_type].append(u)
    return manifest, by_type, rows


def _correct(u: UnitRecord) -> bool:
    if "correct" in u.values:
        return u.values["correct"] >= 1.0
    return u.outcome == "predicted" and u.pred == u.gold


def score_run(root: Path, run_dir: Path, n_resamples: int = 2000) -> dict[str, Any]:
    manifest, by_type, rows = unit_records(root, run_dir)
    tasks_all = load_tasks(root, manifest.dataset.suite, manifest.dataset.split)
    card: dict[str, Any] = {"run_id": manifest.run_id, "system": manifest.system.get("info", {}).get("name"),
                            "model": manifest.model, "split": manifest.dataset.split,
                            "repeats": manifest.repeats, "task_types": {}}
    worst_error = 0.0
    for task_type, units in sorted(by_type.items()):
        pack = get_pack(next(t.pack for t in tasks_all if t.task_type == task_type))
        spec = pack.task_types[task_type]
        overall = {k: v.as_dict() for k, v in bootstrap(units, spec.aggregate, n_resamples).items()}

        strata_keys = sorted({k for u in units for k in u.strata})
        by_stratum: dict[str, dict[str, Any]] = {}
        for key in strata_keys:
            for val in sorted({u.strata.get(key) for u in units if key in u.strata}):
                sub = [u for u in units if u.strata.get(key) == val]
                agg = spec.aggregate(sub)
                by_stratum[f"{key}={val}"] = {
                    m: v for m, v in agg.items()
                    if m == spec.primary_metric or m.endswith(("macro_f1", "ndcg@10", "recall@5", "coverage", "n"))
                }

        stab_input: dict[str, list[bool]] = defaultdict(list)
        for u in sorted(units, key=lambda u: u.extra.get("repeat", 0)):
            if u.level in ("default", spec.levels[-1]):
                stab_input[u.unit_id].append(_correct(u))
        stability = repeat_stability(stab_input) if manifest.repeats > 1 else {}

        trows = [r for r in rows if r["task_type"] == task_type]
        costs = [r["cost_usd"] for r in trows]
        lat = [r["outcome"].get("latency_ms") for r in trows if r["outcome"].get("latency_ms") is not None]
        cost_known = bool(costs) and all(c is not None for c in costs)
        usage = {
            "calls": len(trows),
            "cost_usd_total": float(sum(costs)) if cost_known else None,
            "cost_usd_per_task": float(sum(costs) / len(costs)) if cost_known else None,
            "latency_ms_p50": float(np.percentile(lat, 50)) if lat else None,
            "latency_ms_p95": float(np.percentile(lat, 95)) if lat else None,
            "latency_kind": trows[0]["outcome"].get("latency_kind") if trows else None,
        }
        floors = {}
        if spec.floors:
            floors = spec.floors([t for t in tasks_all if t.task_type == task_type])

        # Errors count at any level. Missing answers count only on the level the system answers
        # best: a system that answers per requirement group leaves every spec unanswered by design.
        errs = [overall[k]["value"] for k in overall if k.endswith("error_rate")]
        missing = [overall[k]["value"] for k in overall if k.endswith("no_prediction_rate")]
        err = max(max(errs, default=0.0), min(missing, default=0.0))
        worst_error = max(worst_error, err)
        card["task_types"][task_type] = {
            "primary_metric": spec.primary_metric,
            "metrics": overall,
            "strata": by_stratum,
            "stability": stability,
            "usage": usage,
            "floors": floors,
            "higher_is_better": spec.higher_is_better,
        }
    status = manifest.status
    if status == "ok" and worst_error > DEGRADED_THRESHOLD:
        status = "degraded"
    card["status"] = status
    card["manifest"] = json.loads(manifest.model_dump_json())
    (run_dir / "scorecard.json").write_text(json.dumps(card, indent=2, default=float))
    return card
