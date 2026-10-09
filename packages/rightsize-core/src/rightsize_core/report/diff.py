"""Compare two runs on the items both scored: paired bootstrap deltas plus what changed."""

from __future__ import annotations

from pathlib import Path

from rightsize_core.packs import get_pack
from rightsize_core.runner.score import unit_records
from rightsize_core.scoring.stats import paired_difference

_MANIFEST_KEYS = ["model", "params", "repeats", "rightsize_git_sha"]


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def manifest_changes(a, b) -> list[str]:
    changes = []
    for k in _MANIFEST_KEYS:
        va, vb = getattr(a, k), getattr(b, k)
        if va != vb:
            changes.append(f"{k}: {va!r} -> {vb!r}")
    fa, fb = _flatten(a.system), _flatten(b.system)
    for k in sorted(set(fa) | set(fb)):
        if k.endswith(("latency_ms",)):
            continue
        if fa.get(k) != fb.get(k):
            changes.append(f"system.{k}: {fa.get(k)!r} -> {fb.get(k)!r}")
    if a.dataset.files != b.dataset.files:
        changes.append("dataset files differ (scores are compared only on shared items)")
    return changes


def diff_data(root: Path, run_a: Path, run_b: Path, delta: float = 0.0, n_resamples: int = 2000) -> dict:
    """Structured paired comparison (B minus A): what changed, and per task type every metric's
    delta with its CI and a flag ("regression", "improved", or "")."""
    from rightsize_core.integrity.dataset import load_suite_config

    ma, ua, _ = unit_records(root, run_a)
    mb, ub, _ = unit_records(root, run_b)
    out: dict = {"a": run_a.name, "b": run_b.name, "changes": manifest_changes(ma, mb), "task_types": {}}
    pack = get_pack(load_suite_config(root, ma.dataset.suite)["pack"])
    for task_type in sorted(set(ua) & set(ub)):
        spec = pack.task_types[task_type]
        d = paired_difference(ua[task_type], ub[task_type], spec.aggregate, n_resamples)
        metrics = {}
        for name, est in sorted(d.items(), key=lambda kv: (kv[0] != spec.primary_metric, kv[0])):
            if name.endswith(".n") or name == "n":
                continue
            better_high = spec.higher_is_better.get(name, True)
            worse = (est.hi < -delta) if better_high else (est.lo > delta)
            better = (est.lo > delta) if better_high else (est.hi < -delta)
            metrics[name] = {"value": est.value, "lo": est.lo, "hi": est.hi,
                             "flag": "regression" if worse else "improved" if better else ""}
        out["task_types"][task_type] = {
            "primary_metric": spec.primary_metric,
            "n_clusters": next(iter(d.values())).n_clusters if d else 0,
            "metrics": metrics,
        }
    return out


def primary_regressions(data: dict) -> list[str]:
    """Task types whose primary metric regressed."""
    return [tt for tt, t in data["task_types"].items()
            if t["metrics"].get(t["primary_metric"], {}).get("flag") == "regression"]


def render_diff(data: dict) -> str:
    lines = [f"A: {data['a']}", f"B: {data['b']}", "", "What changed:"]
    lines += [f"  {c}" for c in data["changes"]] or ["  (nothing recorded in the manifests)"]
    for task_type, t in data["task_types"].items():
        lines += ["", f"{task_type} (B - A, paired on {t['n_clusters']} shared clusters)"]
        for name, m in t["metrics"].items():
            star = "*" if name == t["primary_metric"] else " "
            flag = m["flag"].upper() if m["flag"] == "regression" else m["flag"]
            lines.append(f" {star}{name:<34} {m['value']:+.3f}  [{m['lo']:+.3f}, {m['hi']:+.3f}]  {flag}")
    return "\n".join(lines)


def diff_runs(root: Path, run_a: Path, run_b: Path, delta: float = 0.0, n_resamples: int = 2000) -> str:
    return render_diff(diff_data(root, run_a, run_b, delta, n_resamples))
