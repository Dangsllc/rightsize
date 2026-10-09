"""Scoring for the compliance task types. Everything here is deterministic."""

from __future__ import annotations

import random

from rightsize_core.packs.protocol import UnitRecord
from rightsize_core.schema import Task
from rightsize_core.scoring import classification as clf
from rightsize_core.scoring import ranking

from rightsize_pack_compliance import citations
from rightsize_pack_compliance.models import (
    SCORED_STATUSES,
    ControlPrediction,
    RetrievalPrediction,
)

DEFICIENT = {"gap", "partial"}

# --------------------------------------------------------------------------- retrieval


def score_retrieval(task: Task, pred: RetrievalPrediction | None, outcome: str) -> list[UnitRecord]:
    exp = task.expected
    targets = [r["citation"] for r in exp["relevant"]]
    target_grade = {r["citation"]: r.get("grade", 3) for r in exp["relevant"]}
    corpus_parts = set(exp.get("corpus_parts", []))
    unit = UnitRecord(
        task_id=task.id,
        unit_id=task.id,
        cluster=exp.get("cluster", targets[0]),
        strata=task.strata,
        gold=targets,
        outcome=outcome,
    )
    if outcome != "predicted" or pred is None:
        return [unit]

    k = int(task.inputs.get("k", 10))
    hits = pred.hits[:k]
    grades, cites, out_of_corpus, inferred = [], [], 0, 0
    for h in hits:
        c = citations.parse(h.citation)
        cites.append(c.canonical if c else None)
        if c and corpus_parts and c.part not in corpus_parts:
            out_of_corpus += 1
        if h.citation_source == "inferred":
            inferred += 1
        # A hit's grade is its best grade against any target, capped by that target's own grade.
        g = max((min(citations.grade(c, t), target_grade[t]) for t in targets), default=0) if c else 0
        grades.append(g)

    # An exact target counts once; repeated exact hits on the same target earn nothing more.
    # Partial-credit hits (ancestors, descendants, same standard) are not de-duplicated, so
    # nDCG is capped at 1.0 below.
    found: set[str] = set()
    dedup = []
    for c, g in zip(cites, grades, strict=True):
        exact = next((t for t in targets if c == citations.normalize(t)), None) if c else None
        if exact is not None:
            if exact in found:
                g = 0
            found.add(exact)
        dedup.append(g)

    ideal = [target_grade[t] for t in targets]
    hard_neg = {citations.normalize(n) for n in exp.get("hard_negatives", [])}
    first_target = next((i for i, g in enumerate(dedup) if g == 3), None)
    first_neg = next((i for i, c in enumerate(cites) if c in hard_neg), None)
    confused = float(first_neg is not None and (first_target is None or first_neg < first_target))

    unit.pred = cites
    unit.values = {
        "ndcg@10": min(1.0, ranking.ndcg_at_k(dedup, ideal, 10)),
        "correct": ranking.recall_at_k(dedup, len(targets), 1, 3),
        "recall@1": ranking.recall_at_k(dedup, len(targets), 1, 3),
        "recall@5": ranking.recall_at_k(dedup, len(targets), 5, 3),
        "recall@10": ranking.recall_at_k(dedup, len(targets), 10, 3),
        "recall@5_lenient": ranking.recall_at_k(dedup, len(targets), 5, 1),
        "mrr": ranking.reciprocal_rank(dedup, 3),
        **({"confusion@k": confused} if hard_neg else {}),
        "empty": float(len(hits) == 0),
        "out_of_corpus_share": out_of_corpus / len(hits) if hits else 0.0,
        "inferred_share": inferred / len(hits) if hits else 0.0,
    }
    return [unit]


def aggregate_retrieval(units: list[UnitRecord]) -> dict[str, float]:
    n = len(units)
    if n == 0:
        return {}
    scored = [u for u in units if u.outcome == "predicted"]
    out = {
        "error_rate": sum(u.outcome in ("error", "refusal") for u in units) / n,
        "n": float(n),
    }
    keys = sorted({k for u in scored for k in u.values})
    for key in keys:  # a metric is averaged over the units that define it (e.g. confusion@k)
        vals = [u.values[key] for u in scored if key in u.values]
        out[key] = sum(vals) / len(vals)
    return out


# --------------------------------------------------------------------------- classification


def _group_status(statuses: list[str]) -> str:
    if all(s == "covered" for s in statuses):
        return "covered"
    if all(s == "gap" for s in statuses):
        return "gap"
    return "partial"


def _coarser_match(code: str, group_codes: list[str]) -> str | None:
    """Map an answer given at a coarser paragraph (e.g. 164.312(a)) onto the one requirement
    group it contains (164.312(a)(1)). Ambiguous or unrelated codes map to nothing."""
    c = citations.parse(code)
    if c is None:
        return None
    hits = [g for g in group_codes if (gc := citations.parse(g)) and (c.is_ancestor_of(gc))]
    return hits[0] if len(hits) == 1 else None


def score_control(task: Task, pred: ControlPrediction | None, outcome: str) -> list[UnitRecord]:
    exp_units: dict[str, dict] = task.expected["units"]
    exp_groups: dict[str, dict] = task.expected.get("groups", {})
    cluster = task.inputs.get("document_id", task.id)

    by_code: dict[str, object] = {}
    extras = 0
    if pred is not None:
        for a in pred.assessments:
            code = citations.normalize(a.control_code) or a.control_code
            if code not in exp_units and code not in exp_groups:
                code = _coarser_match(code, list(exp_groups)) or code
            if code in exp_units or code in exp_groups:
                by_code.setdefault(code, a)
            else:
                extras += 1

    units: list[UnitRecord] = []

    def unit_for(code: str, gold: str, level: str, strata: dict) -> UnitRecord:
        u = UnitRecord(
            task_id=task.id, unit_id=f"{task.id}::{code}", cluster=cluster, level=level,
            strata={**task.strata, **strata}, gold=gold,
        )
        if outcome != "predicted" or pred is None:
            u.outcome = outcome
            return u
        a = by_code.get(code)
        if a is None:
            u.outcome = "no_prediction"
            return u
        u.pred = a.status
        u.confidence = a.confidence
        if a.abstained:
            u.outcome = "abstained"
        elif a.status is None:
            u.outcome = "no_prediction"
        return u

    for code, e in exp_units.items():
        units.append(unit_for(code, e["status"], "spec", {
            "label": e["status"], "mutation": e.get("mutation", "intact"),
            "standard_type": e.get("standard_type") or "unknown",
            **({"writer": e["writer"]} if e.get("writer") else {}),
        }))

    for gcode, g in exp_groups.items():
        u = unit_for(gcode, g["status"], "group", {"label": g["status"]})
        if u.outcome == "no_prediction" and outcome == "predicted":
            # No group-level answer: derive one from member answers when all members have one.
            member = [by_code.get(m) for m in g["members"]]
            if member and all(a is not None and a.status and not a.abstained for a in member):
                u.pred = _group_status([a.status for a in member])
                u.outcome = "predicted"
                u.extra["derived"] = True
        units.append(u)

    if units:
        units[0].extra["extras"] = extras
    return units


def _level_metrics(units: list[UnitRecord], prefix: str) -> dict[str, float]:
    n = len(units)
    if n == 0:
        return {}
    labels = list(SCORED_STATUSES) + ["not_applicable"]
    answered = [u for u in units if u.outcome == "predicted"]
    shipped = [u for u in units if u.outcome in ("predicted", "abstained") and u.pred is not None]
    out = {
        f"{prefix}.n": float(n),
        f"{prefix}.coverage": len(answered) / n,
        f"{prefix}.no_prediction_rate": sum(u.outcome == "no_prediction" for u in units) / n,
        f"{prefix}.abstain_rate": sum(u.outcome == "abstained" for u in units) / n,
        f"{prefix}.error_rate": sum(u.outcome in ("error", "refusal") for u in units) / n,
    }
    if answered:
        g = [u.gold for u in answered]
        p = [u.pred for u in answered]
        out[f"{prefix}.macro_f1"] = clf.macro_f1(g, p, labels)
        out[f"{prefix}.accuracy"] = clf.accuracy(g, p)
        out[f"{prefix}.kappa"] = clf.cohen_kappa(g, p, labels)
        ordinal = [(a, b) for a, b in zip(g, p, strict=True) if b in SCORED_STATUSES]
        if ordinal:
            out[f"{prefix}.kappa_linear"] = clf.cohen_kappa(
                [a for a, _ in ordinal], [b for _, b in ordinal], SCORED_STATUSES, weights="linear"
            )
        deficient = [u for u in answered if u.gold in DEFICIENT]
        if deficient:
            out[f"{prefix}.deficiency_recall"] = sum(u.pred in DEFICIENT for u in deficient) / len(deficient)
        for lab, m in clf.per_class(g, p, SCORED_STATUSES).items():
            out[f"{prefix}.f1.{lab}"] = m["f1"]
        conf = [u for u in answered if u.confidence is not None]
        if conf:
            ok = [u.pred == u.gold for u in conf]
            out[f"{prefix}.ece"] = clf.ece([u.confidence for u in conf], ok)
            out[f"{prefix}.brier"] = clf.brier([u.confidence for u in conf], ok)
    if shipped:
        out[f"{prefix}.macro_f1_as_shipped"] = clf.macro_f1(
            [u.gold for u in shipped], [u.pred for u in shipped], labels
        )
    return out


def aggregate_control(units: list[UnitRecord]) -> dict[str, float]:
    out: dict[str, float] = {}
    for level in ("spec", "group"):
        out.update(_level_metrics([u for u in units if u.level == level], level))
    return out


# --------------------------------------------------------------------------- floors


def control_floors(tasks: list[Task]) -> dict[str, dict[str, float]]:
    """What trivial predictors score, so a headline number can be read against them."""

    def run(name: str, pick) -> dict[str, float]:
        units: list[UnitRecord] = []
        for t in tasks:
            codes = list(t.expected["units"]) + list(t.expected.get("groups", {}))
            golds = {**{c: e["status"] for c, e in t.expected["units"].items()},
                     **{c: g["status"] for c, g in t.expected.get("groups", {}).items()}}
            from rightsize_pack_compliance.models import Assessment

            pred = ControlPrediction(assessments=[
                Assessment(control_code=c, status=pick(golds[c])) for c in codes
            ])
            units += score_control(t, pred, "predicted")
        return aggregate_control(units)

    golds = [e["status"] for t in tasks for e in t.expected["units"].values()]
    majority = max(set(golds), key=golds.count) if golds else "covered"
    rng = random.Random(7)
    return {
        "always_covered": run("always_covered", lambda _g: "covered"),
        "always_partial": run("always_partial", lambda _g: "partial"),
        "always_gap": run("always_gap", lambda _g: "gap"),
        f"majority({majority})": run("majority", lambda _g: majority),
        "random": run("random", lambda _g: rng.choice(SCORED_STATUSES)),
    }
