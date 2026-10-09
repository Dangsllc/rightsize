"""Classification metrics. Labels are strings; `None` predictions must be filtered by the caller."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence


def confusion(gold: Sequence[str], pred: Sequence[str], labels: Sequence[str]) -> dict[str, dict[str, int]]:
    m = {g: {p: 0 for p in labels} for g in labels}
    for g, p in zip(gold, pred, strict=True):
        if g in m and p in m[g]:
            m[g][p] += 1
    return m


def per_class(gold: Sequence[str], pred: Sequence[str], labels: Sequence[str]) -> dict[str, dict[str, float]]:
    out = {}
    for lab in labels:
        tp = sum(1 for g, p in zip(gold, pred, strict=True) if g == lab and p == lab)
        fp = sum(1 for g, p in zip(gold, pred, strict=True) if g != lab and p == lab)
        fn = sum(1 for g, p in zip(gold, pred, strict=True) if g == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[lab] = {"precision": prec, "recall": rec, "f1": f1, "support": float(tp + fn)}
    return out


def macro_f1(gold: Sequence[str], pred: Sequence[str], labels: Sequence[str]) -> float:
    """Macro-F1 over the labels present in gold (absent classes would add a meaningless 0)."""
    present = [lab for lab in labels if lab in set(gold)]
    if not present:
        return 0.0
    pc = per_class(gold, pred, labels)
    return sum(pc[lab]["f1"] for lab in present) / len(present)


def accuracy(gold: Sequence[str], pred: Sequence[str]) -> float:
    return sum(g == p for g, p in zip(gold, pred, strict=True)) / len(gold) if gold else 0.0


def cohen_kappa(
    gold: Sequence[str], pred: Sequence[str], labels: Sequence[str], weights: str | None = None
) -> float:
    """Cohen's kappa. `weights="linear"` treats `labels` as an ordinal scale in the given order."""
    n = len(gold)
    if n == 0:
        return 0.0
    idx = {lab: i for i, lab in enumerate(labels)}
    k = len(labels)

    def w(i: int, j: int) -> float:
        if weights == "linear":
            return abs(i - j) / (k - 1) if k > 1 else 0.0
        return 0.0 if i == j else 1.0

    obs = Counter((idx[g], idx[p]) for g, p in zip(gold, pred, strict=True) if g in idx and p in idx)
    gm = Counter(idx[g] for g in gold if g in idx)
    pm = Counter(idx[p] for p in pred if p in idx)
    o = sum(w(i, j) * c for (i, j), c in obs.items()) / n
    e = sum(w(i, j) * gm[i] * pm[j] for i in range(k) for j in range(k)) / (n * n)
    return 1.0 - o / e if e > 0 else (1.0 if o == 0 else 0.0)


def ece(confidences: Sequence[float], correct: Sequence[bool], bins: int = 10) -> float:
    """Expected calibration error with equal-mass bins."""
    pairs = sorted(zip(confidences, correct, strict=True))
    n = len(pairs)
    if n == 0:
        return 0.0
    total = 0.0
    for b in range(bins):
        chunk = pairs[b * n // bins : (b + 1) * n // bins]
        if not chunk:
            continue
        conf = sum(c for c, _ in chunk) / len(chunk)
        acc = sum(1 for _, ok in chunk if ok) / len(chunk)
        total += len(chunk) / n * abs(conf - acc)
    return total


def brier(confidences: Sequence[float], correct: Sequence[bool]) -> float:
    n = len(confidences)
    return sum((c - float(ok)) ** 2 for c, ok in zip(confidences, correct, strict=True)) / n if n else 0.0
