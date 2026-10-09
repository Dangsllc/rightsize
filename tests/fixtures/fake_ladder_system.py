"""A fake system whose quality depends on the model it is given. Used to test sweep/calibrate."""

import hashlib

from rightsize_core.schema import Outcome, SystemInfo, UsageRecord

ACCURACY = {  # model -> query_style -> chance of ranking the target first
    "fake-small": {"exact_term": 0.97, "paraphrase": 0.5, "scenario": 0.2},
    "fake-large": {"exact_term": 0.98, "paraphrase": 0.95, "scenario": 0.95},
}
STYLE_BY_QUERY: dict[str, tuple[str, str]] = {}


class FakeLadder:
    def __init__(self, tasks) -> None:
        for t in tasks:
            STYLE_BY_QUERY[t.inputs["query"]] = (t.strata["query_style"], t.expected["relevant"][0]["citation"])

    def info(self):
        return SystemInfo(name="fake-ladder", task_types=["retrieval"], supports_model_param=True, reports_usage=True)

    async def run(self, task_type, inputs, model, params):
        style, target = STYLE_BY_QUERY[inputs["query"]]
        roll = int(hashlib.sha256(f"{model}|{inputs['query']}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        first = target if roll < ACCURACY[model][style] else "160.103"
        tokens = 1000 if model == "fake-small" else 1200
        usage = None if params.get("hide_usage") else [
            UsageRecord(provider="fake", model=model, input_tokens=tokens, output_tokens=100)]
        return Outcome(prediction={"hits": [{"citation": first, "score": 1.0}]}, usage=usage)


def make():
    from pathlib import Path

    from rightsize_core.integrity.dataset import load_tasks

    return FakeLadder(load_tasks(Path(__file__).resolve().parents[2], "m1", "public", ["retrieval"]))
