import asyncio
from pathlib import Path

import pytest
from rightsize_core.integrity.dataset import load_suite_config, load_tasks
from rightsize_core.runner.run import HeldoutGuardError, execute
from rightsize_core.systems.plugin import PluginSystem
from rightsize_reference_systems.registry import make

ROOT = Path(__file__).resolve().parents[2]


def _heldout_tasks():
    cfg = load_suite_config(ROOT, "m1")
    if not (ROOT / cfg["heldout_dir"]).exists():
        pytest.skip("held-out split not present in this checkout")
    return load_tasks(ROOT, "m1", "heldout", ["retrieval"])[:5]


def test_untrusted_system_never_gets_heldout(tmp_path):
    tasks = _heldout_tasks()
    sysobj = PluginSystem(make("bm25", ROOT), trusted=False)
    with pytest.raises(HeldoutGuardError):
        asyncio.run(execute(ROOT, sysobj, tasks, "m1", "heldout", runs_dir=tmp_path))


def test_heldout_run_dir_has_no_item_text(tmp_path):
    tasks = _heldout_tasks()
    sysobj = PluginSystem(make("bm25", ROOT), trusted=True)
    run_dir = asyncio.run(execute(ROOT, sysobj, tasks, "m1", "heldout", runs_dir=tmp_path))
    blob = "".join(p.read_text() for p in run_dir.iterdir())
    for t in tasks:
        assert t.inputs["query"] not in blob


def test_group_only_answers_are_not_degraded(tmp_path):
    """A system that answers per requirement group leaves specs unanswered by design."""
    from rightsize_core.runner.score import score_run
    from rightsize_core.schema import Outcome

    cfg = load_suite_config(ROOT, "m1")
    tasks = load_tasks(ROOT, "m1", "public", ["control_classification"])[:2]
    by_doc = {t.inputs["document_sha256"]: t.expected for t in tasks}

    class GroupOracle:
        def info(self):
            from rightsize_core.schema import SystemInfo

            return SystemInfo(name="group-oracle", task_types=["control_classification"])

        async def run(self, task_type, inputs, model, params):
            exp = by_doc[inputs["document_sha256"]]
            return Outcome(prediction={"assessments": [
                {"control_code": g, "status": e["status"]} for g, e in exp["groups"].items()]})

    run_dir = asyncio.run(execute(ROOT, PluginSystem(GroupOracle()), tasks, "m1", "public", runs_dir=tmp_path))
    card = score_run(ROOT, run_dir)
    m = card["task_types"]["control_classification"]["metrics"]
    assert m["group.macro_f1"]["value"] == 1.0
    assert card["status"] == "ok", card["status"]
    del cfg
