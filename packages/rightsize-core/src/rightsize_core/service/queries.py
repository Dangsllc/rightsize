"""Read-side questions every surface asks: how did this run do, which items went wrong and why,
what changed between two runs, and which rung a sweep would route each task to."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from rightsize_core.calibration.sweep import MIN_ITEMS
from rightsize_core.packs import ItemView, UnitRecord, ViewBlock, get_pack
from rightsize_core.service.index import Index, NotFound
from rightsize_core.service.models import Flip, ItemRow


def _correct(u: UnitRecord) -> bool:
    if "correct" in u.values:
        return u.values["correct"] >= 1.0
    return u.outcome == "predicted" and u.pred == u.gold


def _spec(task_type: str, suite: str, ix: Index):
    from rightsize_core.integrity.dataset import load_suite_config

    return get_pack(load_suite_config(ix.root, suite)["pack"]).task_types[task_type]


def primary_level(spec) -> str:
    """The unit level a task type's primary metric is computed at (e.g. `group` for group.macro_f1)."""
    prefix = spec.primary_metric.split(".", 1)[0]
    return prefix if prefix in spec.levels else spec.levels[-1]


def _at_level(units: list[UnitRecord], level: str) -> list[UnitRecord]:
    """Units at the primary level when there are any, else whatever level the filter left."""
    at = [u for u in units if u.level == level]
    return at or units


def strata_grid(card_tt: dict) -> list[dict]:
    """The scorecard's per-tier numbers as rows: {stratum, key, value, n, few, metrics{}}."""
    rows = []
    for stratum, vals in card_tt.get("strata", {}).items():
        key, _, val = stratum.partition("=")
        n = next((v for k, v in vals.items() if k == "n" or k.endswith(".n")), None)
        rows.append({"stratum": stratum, "key": key, "value": val, "n": None if n is None else int(n),
                     "few": n is not None and n < MIN_ITEMS,
                     "metrics": {k: v for k, v in vals.items() if not (k == "n" or k.endswith(".n"))}})
    return rows


def run_detail(ix: Index, run_id: str) -> dict[str, Any]:
    """Opening a run scores it if nothing has (as `rightsize report` would); lists never do."""
    status = ix.manifest(run_id)["status"]
    card, why = ix.scorecard(run_id, compute=True) if status != "running" else (None, "still running")
    summary = ix.summary(run_id)
    task_types = {}
    for tt, r in (card or {}).get("task_types", {}).items():
        task_types[tt] = {**r, "grid": strata_grid(r)}
    return {"summary": summary, "card": card, "unscored_reason": why, "task_types": task_types,
            "manifest": ix.manifest(run_id)}


def items(ix: Index, run_id: str, task_type: str, stratum: str | None = None, show: str = "all") -> list[ItemRow]:
    """One row per task: how many of its units the system got right. `show` is all | wrong | errors."""
    manifest, by_type, _ = ix.units(run_id)
    suite, split = manifest.dataset.suite, manifest.dataset.split
    spec = _spec(task_type, suite, ix)
    units = by_type.get(task_type, [])
    if stratum:
        key, _, val = stratum.partition("=")
        units = [u for u in units if u.strata.get(key) == val]
    units = _at_level(units, primary_level(spec))
    tasks = ix.tasks(suite, split)
    by_task: dict[str, list[UnitRecord]] = defaultdict(list)
    for u in units:
        by_task[u.task_id].append(u)
    out = []
    for task_id, us in by_task.items():
        vals = [u.values[spec.primary_metric] for u in us if spec.primary_metric in u.values]
        value = sum(vals) / len(vals) if vals else None
        row = ItemRow(task_id=task_id, strata=tasks[task_id].strata if task_id in tasks else {},
                      units=len(us), correct=sum(_correct(u) for u in us),
                      answered=sum(u.outcome == "predicted" for u in us),
                      errors=sum(u.outcome in ("error", "refusal") for u in us), value=value)
        if row.errors == row.units:
            row.mark = "info"
        elif value is not None:
            row.mark = "ok" if value >= 0.999 else "bad" if value <= 0.0 else "warn"
        else:
            row.mark = "ok" if row.correct == row.units else "bad" if row.correct == 0 else "warn"
        out.append(row)
    if show == "wrong":
        out = [r for r in out if r.mark != "ok"]
    elif show == "errors":
        out = [r for r in out if r.errors]
    order = {"bad": 0, "info": 1, "warn": 2, "ok": 3}
    return sorted(out, key=lambda r: (order[r.mark], r.task_id))


def item_view(ix: Index, run_id: str, task_id: str, repeat: int = 0) -> ItemView:
    manifest, by_type, rows = ix.units(run_id)
    task = ix.tasks(manifest.dataset.suite, manifest.dataset.split).get(task_id)
    if task is None:
        raise NotFound(task_id)
    row = next((r for r in rows if r["task_id"] == task_id and r["repeat"] == repeat), None)
    units = [u for u in by_type.get(task.task_type, [])
             if u.task_id == task_id and u.extra.get("repeat", 0) == repeat]
    spec = _spec(task.task_type, manifest.dataset.suite, ix)
    if spec.item_view is not None:
        return spec.item_view(ix.root, task, row["outcome"] if row else None, units)
    return fallback_view(task, row["outcome"] if row else None, units)


def fallback_view(task, outcome: dict | None, units: list[UnitRecord]) -> ItemView:
    """For packs without an item_view: the task, what was expected, and what came back, verbatim."""
    err = (outcome or {}).get("error")
    right = sum(_correct(u) for u in units)
    blocks = [
        ViewBlock(kind="fields", title="Item", fields=[
            ("task", task.id), ("type", task.task_type), ("split", task.split),
            *[(k, v) for k, v in task.strata.items()],
            ("built by", task.provenance.generator), ("writer", task.provenance.generator_llm or "—"),
        ]),
        ViewBlock(kind="document", title="Expected", text=json.dumps(task.expected, indent=2)[:20000]),
        ViewBlock(kind="document", title="Prediction",
                  text=json.dumps((outcome or {}).get("prediction"), indent=2)[:20000]),
    ]
    if err:
        blocks.insert(1, ViewBlock(kind="notes", title="Error", notes=[f"{err['kind']}: {err['message']}"]))
    return ItemView(task_id=task.id, title=task.id, summary=f"{right} of {len(units)} units right",
                    mark="info" if err else "ok" if right == len(units) else "bad" if right == 0 else "warn",
                    blocks=blocks)


def compare(ix: Index, run_a: str, run_b: str, delta: float = 0.0) -> dict[str, Any]:
    """Paired comparison (B minus A) plus the units whose answer flipped between the runs."""
    from rightsize_core.report.diff import diff_data

    data = diff_data(ix.root, ix.run_dir(run_a), ix.run_dir(run_b), delta)
    ma, ua, _ = ix.units(run_a)
    _, ub, _ = ix.units(run_b)
    flips: dict[str, dict[str, list[Flip]]] = {}
    for tt in sorted(set(ua) & set(ub)):
        level = primary_level(_spec(tt, ma.dataset.suite, ix))

        def share(units, level=level):
            acc: dict[tuple[str, str], list[bool]] = defaultdict(list)
            for u in _at_level(units, level):
                acc[(u.task_id, u.unit_id)].append(_correct(u))
            return {k: sum(v) / len(v) for k, v in acc.items()}

        a, b = share(ua[tt]), share(ub[tt])
        shared = sorted(set(a) & set(b))
        flips[tt] = {
            "regressed": [Flip(task_id=t, unit_id=u, a_correct=a[(t, u)], b_correct=b[(t, u)])
                          for t, u in shared if a[(t, u)] >= 0.5 > b[(t, u)]],
            "improved": [Flip(task_id=t, unit_id=u, a_correct=a[(t, u)], b_correct=b[(t, u)])
                         for t, u in shared if b[(t, u)] >= 0.5 > a[(t, u)]],
        }
    card_a = ix.scorecard(run_a)[0] or {}
    sections = []
    for tt, t in data["task_types"].items():
        hib = card_a.get("task_types", {}).get(tt, {}).get("higher_is_better", {})
        rows = [{"name": name, **m, "primary": name == t["primary_metric"], "higher_is_better": hib.get(name, True)}
                for name, m in t["metrics"].items()]
        # The forest shows the primary metric and anything flagged, topped up to ten rows.
        shown = [r for r in rows if r["primary"] or r["flag"]]
        shown += [r for r in rows if r not in shown][: max(0, 10 - len(shown))]
        sections.append({"task_type": tt, "primary": t["primary_metric"], "n": t["n_clusters"], "forest": shown,
                         "rows": rows, "flips": flips.get(tt, {})})
    return {"a": ix.summary(run_a), "b": ix.summary(run_b), "diff": data, "flips": flips, "sections": sections}


def suite_overview(root, suite: str) -> dict[str, Any]:
    """What a suite holds: item counts per task type and split, how public items spread over each
    tier, the classification label mix, items the build couldn't make, and whether its data checks
    out. Held-out items are counted, never listed."""
    from collections import Counter

    from rightsize_core.integrity.dataset import check_suite, heldout_dir, load_suite_config, load_tasks, suite_dir

    cfg = load_suite_config(root, suite)
    sdir = suite_dir(root, suite)
    manifest = json.loads((sdir / "MANIFEST.json").read_text()) if (sdir / "MANIFEST.json").exists() else {}
    build = json.loads((sdir / "BUILD_REPORT.json").read_text()) if (sdir / "BUILD_REPORT.json").exists() else {}
    tiers: dict[str, dict[str, dict[str, int]]] = {}
    labels: dict[str, dict[str, int]] = {}
    try:
        public = load_tasks(root, suite, "public")
    except FileNotFoundError:
        public = []
    for t in public:
        for k, v in t.strata.items():
            tiers.setdefault(t.task_type, {}).setdefault(k, Counter())[v] += 1
        for level in ("groups", "units"):
            for e in (t.expected.get(level) or {}).values():
                if isinstance(e, dict) and "status" in e:
                    labels.setdefault(level, Counter())[e["status"]] += 1
    heldout = None
    if heldout_dir(root, cfg).is_dir():
        try:
            heldout = dict(Counter(t.task_type for t in load_tasks(root, suite, "heldout")))
        except FileNotFoundError:
            heldout = None
    missing = {tt: len(r.get("missing", [])) for tt, r in build.items() if isinstance(r, dict) and "missing" in r}
    return {"suite": suite, "pack": cfg.get("pack"), "edition": cfg.get("edition"), "canary": cfg.get("canary"),
            "counts": manifest.get("counts", {}), "heldout_counts": heldout, "check": check_suite(root, suite),
            "tiers": {tt: {k: dict(sorted(v.items())) for k, v in ks.items()} for tt, ks in tiers.items()},
            "labels": {k: dict(v) for k, v in labels.items()}, "not_built": missing}


def calibration(ix: Index, sweep_id: str) -> dict[str, Any] | None:
    """calibration.json plus, per task type, the overall-tier points with the Pareto frontier marked."""
    f = ix.sweep_dir(sweep_id) / "calibration.json"
    if not f.exists():
        return None
    cal = json.loads(f.read_text())
    charts = {}
    for d in cal["decisions"]:
        if d["stratum"] != "all":
            continue
        pts = [{"rung": name, **{k: v for k, v in r.items() if k != "metrics"}} for name, r in d["rungs"].items()]
        frontier, best = set(), float("-inf")
        for p in sorted((p for p in pts if p["cost"] is not None), key=lambda p: (p["cost"], -p["value"])):
            if p["value"] > best:
                frontier.add(p["rung"])
                best = p["value"]
        for p in pts:
            p["frontier"] = p["rung"] in frontier
            p["chosen"] = p["rung"] == d["chosen"]
        charts[d["task_type"]] = {"metric": d["metric"], "points": pts, "chosen": d["chosen"], "best": d["best"]}
    return {**cal, "charts": charts}
