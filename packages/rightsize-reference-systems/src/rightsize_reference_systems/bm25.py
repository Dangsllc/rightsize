"""BM25 over the benchmark's own copy of 45 CFR 160/164. Deterministic and free."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi
from rightsize_core.schema import Outcome, OutcomeError, SystemInfo, UsageRecord
from rightsize_pack_compliance.gen.text import STOPWORDS, words
from rightsize_pack_compliance.sources.ecfr import load_jsonl

from rightsize_reference_systems.base import ReferenceSystem


def _tok(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS]


class BM25System(ReferenceSystem):
    def __init__(self, sources: list[Path]) -> None:
        self.docs: list[dict] = []
        for p in sources:
            for r in load_jsonl(p):
                text = f"{r['title']} {r['text']}".strip()
                if text:
                    self.docs.append({"citation": r["control_code"], "text": text})
        self.index = BM25Okapi([_tok(d["text"]) for d in self.docs])

    def info(self) -> SystemInfo:
        return SystemInfo(name="bm25", version="0.1.0", task_types=["retrieval"],
                          granularity={"retrieval": "spec"}, deterministic=True, reports_usage=True)

    async def run(self, task_type: str, inputs: dict[str, Any], model: str | None,
                  params: dict[str, Any]) -> Outcome:
        if task_type != "retrieval":
            return Outcome(error=OutcomeError(kind="unsupported", message=task_type))
        t0 = time.monotonic()
        k = int(inputs.get("k", 10))
        scores = self.index.get_scores(_tok(inputs["query"]))
        top = sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:k]
        hits = [{"citation": self.docs[i]["citation"], "score": float(scores[i])} for i in top]
        return Outcome(prediction={"hits": hits}, latency_ms=int((time.monotonic() - t0) * 1000),
                       latency_kind="model", params_sent={"k": k},
                       usage=[UsageRecord(provider="local", model="bm25", call_role="retrieve")])
