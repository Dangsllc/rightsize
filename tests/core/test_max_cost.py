"""`--max-cost` must stop new calls, not just label the run aborted afterwards."""

import asyncio
import json
from pathlib import Path

import yaml
from rightsize_core.integrity.dataset import load_tasks
from rightsize_core.runner.run import execute
from rightsize_core.schema import Outcome, SystemInfo, UsageRecord
from rightsize_core.systems.plugin import PluginSystem
from rightsize_core.usage.pricing import Pricing

ROOT = Path(__file__).resolve().parents[2]


class OneDollarPerCall:
    """Every call bills exactly $1 (1M input tokens at $1/MTok)."""

    def __init__(self) -> None:
        self.calls = 0

    def info(self):
        return SystemInfo(name="one-dollar", task_types=["retrieval"], supports_model_param=True, reports_usage=True)

    async def run(self, task_type, inputs, model, params):
        self.calls += 1
        await asyncio.sleep(0.001)
        return Outcome(prediction={"hits": [{"citation": "160.103"}]},
                       usage=[UsageRecord(provider="fake", model="dollar", input_tokens=1_000_000)])


async def test_max_cost_stops_new_calls(tmp_path):
    pricing_file = tmp_path / "pricing.yaml"
    pricing_file.write_text(yaml.safe_dump({"as_of": "test", "models": {"dollar": {"input": 1.0, "output": 0.0}}}))
    impl = OneDollarPerCall()
    system = PluginSystem(impl)
    system.configure("dollar", {})
    tasks = load_tasks(ROOT, "m1", "public", ["retrieval"])
    cap, concurrency = 3.0, 2
    run_dir = await execute(ROOT, system, tasks, "m1", "public", concurrency=concurrency,
                            runs_dir=tmp_path / "runs", pricing=Pricing(pricing_file), max_cost=cap)
    rows = (run_dir / "results.jsonl").read_text().splitlines()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "aborted"
    # The call that crosses the cap finishes, and so may the others already in flight.
    assert impl.calls <= cap + concurrency, f"{impl.calls} calls went out under a ${cap} cap"
    assert len(rows) == impl.calls
