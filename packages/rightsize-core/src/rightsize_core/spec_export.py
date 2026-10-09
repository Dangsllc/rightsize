"""Export the rightsize/v1 JSON Schemas from the pydantic models (the code is the source of truth)."""

from __future__ import annotations

import json
from pathlib import Path

from rightsize_core.packs import get_pack
from rightsize_core.schema import Outcome, RunRequest, SystemInfo, Task


def schemas() -> dict[str, dict]:
    out = {
        "task.schema.json": Task.model_json_schema(),
        "system-info.schema.json": SystemInfo.model_json_schema(),
        "run-request.schema.json": RunRequest.model_json_schema(),
        "outcome.schema.json": Outcome.model_json_schema(),
    }
    pack = get_pack("compliance")
    for name, spec in pack.task_types.items():
        out[f"compliance.{name}.input.schema.json"] = spec.input_model.model_json_schema()
        out[f"compliance.{name}.prediction.schema.json"] = spec.prediction_model.model_json_schema()
    return out


def export(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, schema in sorted(schemas().items()):
        p = dest / name
        p.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        paths.append(p)
    return paths
