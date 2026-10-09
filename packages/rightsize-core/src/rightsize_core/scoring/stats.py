"""Uncertainty for any metric: cluster bootstrap, paired differences, and repeat stability.

Everything takes unit records plus the pack's `aggregate` function, so the same code produces
CIs for nDCG, macro-F1, or a metric a future pack invents.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from rightsize_core.packs.protocol import UnitRecord

Aggregate = Callable[[list[UnitRecord]], dict[str, float]]


@dataclass
class Estimate:
    value: float
    lo: float
    hi: float
    n_clusters: int

    def as_dict(self) -> dict[str, float]:
        return {"value": self.value, "lo": self.lo, "hi": self.hi, "n_clusters": self.n_clusters}


def _by_cluster(units: Sequence[UnitRecord]) -> dict[str, list[UnitRecord]]:
    groups: dict[str, list[UnitRecord]] = defaultdict(list)
    for u in units:
        groups[u.cluster].append(u)
    return groups


def bootstrap(
    units: Sequence[UnitRecord],
    aggregate: Aggregate,
    n_resamples: int = 2000,
    seed: int = 20261003,
    alpha: float = 0.05,
) -> dict[str, Estimate]:
    """Point estimate on all units, percentile CI from resampling whole clusters.

    Repeats of the same item share a cluster, so repeated runs do not shrink the interval.
    """
    point = aggregate(list(units))
    groups = _by_cluster(units)
    keys = sorted(groups)
    if not keys:
        return {}
    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = defaultdict(list)
    for _ in range(n_resamples):
        pick = rng.integers(0, len(keys), len(keys))
        res = [u for i in pick for u in groups[keys[i]]]
        for name, val in aggregate(res).items():
            samples[name].append(val)
    out = {}
    for name, val in point.items():
        s = np.asarray(samples.get(name, [val]))
        out[name] = Estimate(
            value=float(val),
            lo=float(np.quantile(s, alpha / 2)),
            hi=float(np.quantile(s, 1 - alpha / 2)),
            n_clusters=len(keys),
        )
    return out


def paired_difference(
    units_a: Sequence[UnitRecord],
    units_b: Sequence[UnitRecord],
    aggregate: Aggregate,
    n_resamples: int = 2000,
    seed: int = 20261003,
    alpha: float = 0.05,
) -> dict[str, Estimate]:
    """B minus A on the clusters both runs scored, resampling the same clusters for both."""
    ga, gb = _by_cluster(units_a), _by_cluster(units_b)
    keys = sorted(set(ga) & set(gb))
    if not keys:
        return {}

    def agg(groups, pick):
        return aggregate([u for i in pick for u in groups[keys[i]]])

    all_idx = np.arange(len(keys))
    pa, pb = agg(ga, all_idx), agg(gb, all_idx)
    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = defaultdict(list)
    for _ in range(n_resamples):
        pick = rng.integers(0, len(keys), len(keys))
        ra, rb = agg(ga, pick), agg(gb, pick)
        for name in pa:
            if name in rb:
                samples[name].append(rb[name] - ra[name])
    out = {}
    for name in pa:
        if name not in pb:
            continue
        s = np.asarray(samples[name])
        out[name] = Estimate(
            value=float(pb[name] - pa[name]),
            lo=float(np.quantile(s, alpha / 2)),
            hi=float(np.quantile(s, 1 - alpha / 2)),
            n_clusters=len(keys),
        )
    return out


def repeat_stability(correct_by_repeat: dict[str, Sequence[bool]], k: int | None = None) -> dict[str, float]:
    """pass@1 (mean single-repeat accuracy), pass^k (all k repeats right), and flip rate.

    `correct_by_repeat` maps a unit id to its per-repeat correctness.
    """
    if not correct_by_repeat:
        return {}
    pass1, passk, flips = [], [], []
    for runs in correct_by_repeat.values():
        runs = list(runs)[: k or None]
        if not runs:
            continue
        pass1.append(sum(runs) / len(runs))
        passk.append(float(all(runs)))
        flips.append(float(len(set(runs)) > 1))
    return {
        "pass@1": float(np.mean(pass1)),
        "pass^k": float(np.mean(passk)),
        "flip_rate": float(np.mean(flips)),
        "k": float(k or max(len(v) for v in correct_by_repeat.values())),
    }
