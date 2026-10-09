import math

import pytest
from rightsize_core.packs.protocol import UnitRecord
from rightsize_core.scoring import classification as clf
from rightsize_core.scoring import ranking
from rightsize_core.scoring.stats import bootstrap, paired_difference, repeat_stability


def test_dcg_and_ndcg_by_hand():
    # grades [3, 0, 1]: (2^3-1)/log2(2) + 0 + (2^1-1)/log2(4) = 7 + 0.5
    assert ranking.dcg([3, 0, 1], 10) == pytest.approx(7.5)
    # ideal for one target of grade 3 is 7
    assert ranking.ndcg_at_k([3, 0, 1], [3], 10) == pytest.approx(7.5 / 7)
    assert ranking.ndcg_at_k([0, 3], [3], 10) == pytest.approx((7 / math.log2(3)) / 7)
    assert ranking.ndcg_at_k([0, 0], [3], 10) == 0.0
    assert ranking.ndcg_at_k([3], [], 10) == 0.0


def test_recall_and_mrr():
    assert ranking.recall_at_k([0, 3, 0], 1, 1, 3) == 0.0
    assert ranking.recall_at_k([0, 3, 0], 1, 5, 3) == 1.0
    assert ranking.recall_at_k([1, 0], 1, 5, 1) == 1.0
    assert ranking.recall_at_k([3, 3, 3], 2, 5, 3) == 1.0  # capped
    assert ranking.reciprocal_rank([0, 0, 3], 3) == pytest.approx(1 / 3)
    assert ranking.reciprocal_rank([1, 2], 3) == 0.0


def test_per_class_and_macro_f1_by_hand():
    gold = ["gap", "gap", "covered", "covered", "partial"]
    pred = ["gap", "covered", "covered", "covered", "gap"]
    labels = ["gap", "partial", "covered"]
    pc = clf.per_class(gold, pred, labels)
    # gap: tp1 fp1 fn1 -> p=.5 r=.5 f1=.5 ; covered: tp2 fp1 fn0 -> p=2/3 r=1 f1=.8 ; partial: 0
    assert pc["gap"]["f1"] == pytest.approx(0.5)
    assert pc["covered"]["f1"] == pytest.approx(0.8)
    assert pc["partial"]["f1"] == 0.0
    assert clf.macro_f1(gold, pred, labels) == pytest.approx((0.5 + 0.8 + 0.0) / 3)
    assert clf.accuracy(gold, pred) == pytest.approx(3 / 5)


def test_macro_f1_ignores_absent_gold_classes():
    assert clf.macro_f1(["gap", "gap"], ["gap", "gap"], ["gap", "partial", "covered"]) == 1.0


def test_kappa_by_hand():
    # 2x2: agree 20+15, disagree 5+10, n=50. po=.7 ; pe = (25*30 + 25*20)/2500 = .5 ; k=.4
    gold = ["a"] * 25 + ["b"] * 25
    pred = ["a"] * 20 + ["b"] * 5 + ["a"] * 10 + ["b"] * 15
    assert clf.cohen_kappa(gold, pred, ["a", "b"]) == pytest.approx(0.4)
    assert clf.cohen_kappa(gold, gold, ["a", "b"]) == pytest.approx(1.0)


def test_linear_weighted_kappa_penalizes_distance():
    labels = ["gap", "partial", "covered"]
    gold = ["gap", "covered", "partial", "gap"]
    near = ["partial", "partial", "partial", "gap"]
    far = ["covered", "gap", "partial", "gap"]
    assert clf.cohen_kappa(gold, near, labels, "linear") > clf.cohen_kappa(gold, far, labels, "linear")


def test_ece_and_brier():
    assert clf.ece([1.0, 1.0], [True, True]) == 0.0
    assert clf.ece([1.0, 1.0], [False, False]) == pytest.approx(1.0)
    assert clf.ece([0.8] * 10, [True] * 8 + [False] * 2, bins=1) == pytest.approx(0.0)
    assert clf.brier([1.0, 0.0], [True, False]) == 0.0
    assert clf.brier([0.5], [True]) == pytest.approx(0.25)


def _units(values, clusters):
    return [
        UnitRecord(task_id=str(i), unit_id=str(i), cluster=c, values={"m": v})
        for i, (v, c) in enumerate(zip(values, clusters))
    ]


def _mean(units):
    return {"m": sum(u.values["m"] for u in units) / len(units)} if units else {}


def test_bootstrap_ci_contains_point_and_is_deterministic():
    units = _units([0.0, 1.0] * 50, [f"c{i}" for i in range(100)])
    a = bootstrap(units, _mean, n_resamples=500)
    b = bootstrap(units, _mean, n_resamples=500)
    assert a["m"].value == pytest.approx(0.5)
    assert a["m"].lo < 0.5 < a["m"].hi
    assert (a["m"].lo, a["m"].hi) == (b["m"].lo, b["m"].hi)


def test_bootstrap_clusters_repeats():
    # 3 repeats of 20 items: the CI must be as wide as 20 clusters, not 60 independent units.
    vals = [float(i % 2) for i in range(20)]
    one = _units(vals, [f"c{i}" for i in range(20)])
    three = _units(vals * 3, [f"c{i}" for i in range(20)] * 3)
    w1 = bootstrap(one, _mean, n_resamples=1000)["m"]
    w3 = bootstrap(three, _mean, n_resamples=1000)["m"]
    assert (w3.hi - w3.lo) == pytest.approx(w1.hi - w1.lo, abs=0.05)


def test_paired_difference_detects_improvement():
    clusters = [f"c{i}" for i in range(60)]
    a = _units([0.5] * 60, clusters)
    b = _units([0.7] * 60, clusters)
    d = paired_difference(a, b, _mean, n_resamples=300)["m"]
    assert d.value == pytest.approx(0.2)
    assert d.lo > 0


def test_repeat_stability():
    r = repeat_stability({"u1": [True, True, True], "u2": [True, False, True], "u3": [False] * 3})
    assert r["pass@1"] == pytest.approx((1 + 2 / 3 + 0) / 3)
    assert r["pass^k"] == pytest.approx(1 / 3)
    assert r["flip_rate"] == pytest.approx(1 / 3)
