"""An answer key wearing a system costume. Only for checking that the harness scores 1.0 when
everything is right; it reads the expected labels from the suite files, which a real system
never sees."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from rightsize_core.integrity.dataset import load_tasks
from rightsize_core.schema import Outcome, OutcomeError, SystemInfo

from rightsize_reference_systems.base import ReferenceSystem


class OracleSystem(ReferenceSystem):
    def __init__(self, root: Path, suite: str, split: str = "public") -> None:
        self.by_query: dict[str, dict] = {}
        self.by_doc: dict[str, dict] = {}
        for t in load_tasks(root, suite, split):
            if t.task_type == "retrieval":
                self.by_query[t.inputs["query"]] = t.expected
            elif t.task_type == "control_classification":
                self.by_doc[t.inputs["document_sha256"]] = t.expected

    def info(self) -> SystemInfo:
        return SystemInfo(name="oracle", version="test", task_types=["retrieval", "control_classification"],
                          deterministic=True)

    async def run(self, task_type: str, inputs: dict[str, Any], model: str | None,
                  params: dict[str, Any]) -> Outcome:
        if task_type == "retrieval":
            exp = self.by_query.get(inputs["query"])
            if exp is None:
                return Outcome(error=OutcomeError(kind="error", message="unknown query"))
            return Outcome(prediction={"hits": [{"citation": r["citation"], "score": 1.0} for r in exp["relevant"]]})
        sha = inputs.get("document_sha256") or hashlib.sha256(inputs["document"].encode()).hexdigest()
        exp = self.by_doc.get(sha)
        if exp is None:
            return Outcome(error=OutcomeError(kind="error", message="unknown document"))
        return Outcome(prediction={"assessments": [
            {"control_code": c, "status": e["status"], "confidence": 1.0} for c, e in exp["units"].items()
        ]})
