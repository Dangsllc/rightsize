"""Run a suite against a system, then score it.

Execution and scoring are separate: `execute` writes results.jsonl (one row per task x repeat),
and `score` reads it back. `rightsize report` re-scores an existing run without calling the system.
"""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from rightsize_core import __version__
from rightsize_core.integrity.dataset import load_suite_config, resolve_document, suite_dir
from rightsize_core.packs import get_pack
from rightsize_core.schema import DatasetRef, Outcome, OutcomeError, RunManifest, Task
from rightsize_core.systems.base import System
from rightsize_core.usage.pricing import Pricing


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha(root: Path) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _slug(s: str | None) -> str:
    return "".join(c if c.isalnum() or c in "-." else "-" for c in (s or "default"))[:40]


class HeldoutGuardError(RuntimeError):
    pass


async def execute(
    root: Path,
    system: System,
    tasks: list[Task],
    suite: str,
    split: str,
    repeats: int = 1,
    concurrency: int = 4,
    runs_dir: Path | None = None,
    pricing: Pricing | None = None,
    sweep_id: str | None = None,
    max_cost: float | None = None,
    progress: Any = None,
) -> Path:
    if split == "heldout" and not system.trusted:
        raise HeldoutGuardError(
            "Refusing to send held-out tasks to a system not marked `trusted: true`. "
            "Held-out data sent to a third party is no longer held out."
        )
    cfg = load_suite_config(root, suite)
    pack = get_pack(cfg["pack"])
    info = await system.info()
    unsupported = sorted({t.task_type for t in tasks} - set(info.task_types))
    tasks = [t for t in tasks if t.task_type in info.task_types]
    if not tasks:
        raise RuntimeError(f"System {info.name!r} supports none of the task types in this run")

    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{_slug(info.name)}_{_slug(system.model)}_{uuid.uuid4().hex[:6]}"
    out = (runs_dir or root / "runs") / run_id
    out.mkdir(parents=True)

    mfile = suite_dir(root, suite) / "MANIFEST.json"
    dataset_files = json.loads(mfile.read_text())["files"] if mfile.exists() else {}
    manifest = RunManifest(
        run_id=run_id,
        rightsize_version=__version__,
        rightsize_git_sha=_git_sha(root),
        started_at=_now(),
        dataset=DatasetRef(suite=suite, split=split, files=dataset_files, canary=cfg.get("canary"),
                           task_count=len(tasks)),
        system={**system.describe(), "info": info.model_dump(mode="json")},
        model=system.model,
        params=system.params,
        repeats=repeats,
        task_types=sorted({t.task_type for t in tasks}),
        pricing=pricing.describe() if pricing else None,
        host={"python": sys.version.split()[0], "platform": platform.platform()},
        sweep_id=sweep_id,
        notes=[f"unsupported task types skipped: {unsupported}"] if unsupported else [],
    )
    (out / "manifest.json").write_text(manifest.model_dump_json(indent=2))

    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    spent = 0.0
    aborted = False
    results_path = out / "results.jsonl"
    heldout = split == "heldout"

    async def one(task: Task, rep: int) -> None:
        nonlocal spent, aborted
        if aborted:
            return
        inputs = dict(task.inputs)
        if "document_ref" in inputs:
            inputs["document"] = resolve_document(root, cfg, task)
            inputs.pop("document_ref")
        async with sem:
            outcome = await system.run(task.task_type, inputs)
        spec = pack.task_types[task.task_type]
        if outcome.prediction is not None and outcome.error is None:
            try:
                spec.prediction_model.model_validate(outcome.prediction)
            except ValidationError as e:
                outcome = outcome.model_copy(update={"error": OutcomeError(kind="mapping", message=str(e)[:500])})
        cost = pricing.cost(outcome.usage) if pricing else None
        row = {
            "task_id": task.id, "task_type": task.task_type, "repeat": rep,
            "outcome": outcome.model_dump(mode="json", exclude={"raw"}),
            "cost_usd": cost,
        }
        if heldout:  # scores only: drop anything that could echo held-out document text
            row["outcome"].pop("raw", None)
            pred = row["outcome"].get("prediction") or {}
            for items in pred.values():
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict):
                            it.pop("evidence", None)
                            it.pop("text", None)
        async with lock:
            with results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if cost:
                spent += cost
                if max_cost is not None and spent > max_cost:
                    aborted = True
            if progress:
                progress()

    await system.setup()
    try:
        await asyncio.gather(*(one(t, r) for r in range(repeats) for t in tasks))
    finally:
        await system.teardown()

    manifest.ended_at = _now()
    manifest.status = "aborted" if aborted else "ok"
    if aborted:
        manifest.notes.append(f"aborted: spend ${spent:.2f} exceeded --max-cost {max_cost}")
    (out / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    return out


def load_results(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines() if line.strip()]


def outcome_kind(o: Outcome) -> str:
    if o.error is None:
        return "predicted"
    return {"refusal": "refusal"}.get(o.error.kind, "error")
