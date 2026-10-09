"""Ranking metrics over graded relevance. Grades are non-negative ints; 0 means not relevant."""

from __future__ import annotations

import math
from collections.abc import Sequence


def dcg(grades: Sequence[float], k: int) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades[:k]))


def ndcg_at_k(ranked_grades: Sequence[float], ideal_grades: Sequence[float], k: int) -> float:
    """nDCG@k. `ideal_grades` lists the grade of every relevant item that could be retrieved."""
    ideal = dcg(sorted(ideal_grades, reverse=True), k)
    return dcg(ranked_grades, k) / ideal if ideal > 0 else 0.0


def recall_at_k(ranked_grades: Sequence[float], n_relevant: int, k: int, min_grade: float) -> float:
    """Share of the relevant targets found in the top k, counting hits graded >= min_grade.

    Each ranked position counts at most once, so this is capped at 1.0.
    """
    if n_relevant <= 0:
        return 0.0
    found = sum(1 for g in ranked_grades[:k] if g >= min_grade)
    return min(found, n_relevant) / n_relevant


def reciprocal_rank(ranked_grades: Sequence[float], min_grade: float) -> float:
    for i, g in enumerate(ranked_grades):
        if g >= min_grade:
            return 1.0 / (i + 1)
    return 0.0
