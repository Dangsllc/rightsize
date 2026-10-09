from __future__ import annotations

from rightsize_core.packs.protocol import TaskTypeSpec

from rightsize_pack_compliance import citations
from rightsize_pack_compliance.models import (
    ControlInput,
    ControlPrediction,
    RetrievalInput,
    RetrievalPrediction,
)
from rightsize_pack_compliance.scorers import (
    aggregate_control,
    aggregate_retrieval,
    control_floors,
    score_control,
    score_retrieval,
)


class CompliancePack:
    name = "compliance"
    version = "0.1.0"

    def __init__(self) -> None:
        self.task_types = {
            "retrieval": TaskTypeSpec(
                name="retrieval",
                input_model=RetrievalInput,
                prediction_model=RetrievalPrediction,
                score_item=score_retrieval,
                aggregate=aggregate_retrieval,
                primary_metric="ndcg@10",
                higher_is_better={"confusion@k": False, "empty": False, "error_rate": False},
            ),
            "control_classification": TaskTypeSpec(
                name="control_classification",
                input_model=ControlInput,
                prediction_model=ControlPrediction,
                score_item=score_control,
                aggregate=aggregate_control,
                primary_metric="group.macro_f1",
                levels=["spec", "group"],
                floors=control_floors,
                higher_is_better={
                    "spec.ece": False, "group.ece": False, "spec.brier": False, "group.brier": False,
                    "spec.error_rate": False, "group.error_rate": False,
                    "spec.no_prediction_rate": False, "group.no_prediction_rate": False,
                },
            ),
        }

    @property
    def transforms(self) -> dict:
        """Named functions declarative system configs can call (see rightsize_core.systems.declarative)."""
        return {
            "citation": citations.normalize,
            "citations_in_text": lambda text: [
                {"citation": c, "citation_source": "inferred"} for c in citations.find_in_text(text)
            ],
        }

    def conformance_metrics(self, task, prediction) -> dict:
        """How much of a response maps onto the contract (used by `rightsize conformance`)."""
        if task.task_type == "retrieval":
            hits = prediction.hits
            if not hits:
                return {"mapped_share": 0.0, "inferred_share": 0.0, "items": 0}
            mapped = sum(citations.normalize(h.citation) is not None for h in hits)
            return {"mapped_share": mapped / len(hits),
                    "inferred_share": sum(h.citation_source == "inferred" for h in hits) / len(hits),
                    "items": len(hits)}
        units = score_control(task, prediction, "predicted")
        shares = {}
        for level in ("spec", "group"):
            lv = [u for u in units if u.level == level]
            if lv:
                shares[level] = sum(u.outcome in ("predicted", "abstained") for u in lv) / len(lv)
        best = max(shares, key=shares.get) if shares else "spec"
        return {"mapped_share": shares.get(best, 0.0), "level": best,
                "items": len(prediction.assessments), "extras": units[0].extra.get("extras", 0) if units else 0}

    def normalize_citation(self, raw: str) -> str | None:
        return citations.normalize(raw)

    def build_suite(self, root, cfg, allow_missing_seeds: bool = False) -> dict:
        from rightsize_pack_compliance.build import build_suite

        return build_suite(root, cfg, allow_missing_seeds=allow_missing_seeds)


PACK = CompliancePack()
