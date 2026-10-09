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


def diff_runs(root: Path, run_a: Path, run_b: Path, delta: float = 0.0, n_resamples: int = 2000) -> str:
    ma, ua, _ = unit_records(root, run_a)
    mb, ub, _ = unit_records(root, run_b)
    lines = [f"A: {run_a.name}", f"B: {run_b.name}", "", "What changed:"]
    changes = manifest_changes(ma, mb)
    lines += [f"  {c}" for c in changes] or ["  (nothing recorded in the manifests)"]
    for task_type in sorted(set(ua) & set(ub)):
        units_a, units_b = ua[task_type], ub[task_type]
        from rightsize_core.integrity.dataset import load_suite_config

        pack = get_pack(load_suite_config(root, ma.dataset.suite)["pack"])
        spec = pack.task_types[task_type]
        d = paired_difference(units_a, units_b, spec.aggregate, n_resamples)
        lines += ["", f"{task_type} (B - A, paired on {next(iter(d.values())).n_clusters if d else 0} shared clusters)"]
        for name, est in sorted(d.items(), key=lambda kv: (kv[0] != spec.primary_metric, kv[0])):
            if name.endswith(".n") or name == "n":
                continue
            better_high = spec.higher_is_better.get(name, True)
            worse = (est.hi < -delta) if better_high else (est.lo > delta)
            better = (est.lo > delta) if better_high else (est.hi < -delta)
            flag = "REGRESSION" if worse else "improved" if better else ""
            star = "*" if name == spec.primary_metric else " "
            lines.append(f" {star}{name:<34} {est.value:+.3f}  [{est.lo:+.3f}, {est.hi:+.3f}]  {flag}")
    return "\n".join(lines)
