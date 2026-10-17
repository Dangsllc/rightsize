"""Connect a system: write its config with the problems pointed out line by line, try one item and
see every response, test JSONPath against them, run conformance, and save.

Drafts run without being saved. Saved configs go to configs/systems/local/ (gitignored), so a config
for someone's own system isn't committed to the public repo by accident. Trying an item and
conformance use public items only, and make real calls to the system.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml
from rightsize_core.systems.config_schema import locate, validate_config

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
TRY_TIMEOUT_S = 180
CONFORMANCE_TIMEOUT_S = 900
SHOW_CHARS = 20000
_TRACES: OrderedDict[str, dict[str, Any]] = OrderedDict()  # trace id -> {responses, result}, newest last


def templates(root: Path) -> list[dict[str, str]]:
    """The shipped example configs, each with the comment that opens it."""
    out = []
    for p in sorted((root / "configs" / "systems" / "examples").glob("*.yaml")):
        text = p.read_text()
        lead = [ln.lstrip("# ").rstrip() for ln in text.splitlines() if ln.startswith("#")][:3]
        out.append({"name": p.stem, "path": p.relative_to(root).as_posix(), "about": " ".join(lead), "text": text})
    return out


BLANK = """# A system that answers retrieval questions through its own HTTP API.
name: my-system
transport: http
base_url: ${MY_SYSTEM_URL}
auth: {type: bearer, env: MY_SYSTEM_TOKEN}
trusted: false
tasks:
  retrieval:
    steps:
      - call: POST /search
        body: {query: "{{ inputs.query }}", top_k: "{{ inputs.k }}"}
    extract:
      hits:
        each: "$.results[*]"
        fields:
          citation: {path: $.citation, transform: citation}
          score: {path: $.score, type: float}
  control_classification: unsupported
"""


def problems(text: str) -> list[dict[str, Any]]:
    return [p.as_dict() for p in validate_config(text)]


def _cfg(text: str) -> dict:
    cfg = yaml.safe_load(text)
    if not isinstance(cfg, dict):
        raise ValueError("The config isn't a YAML mapping.")  # noqa: TRY004 - shown to the user as is
    return cfg


def _show(data: Any) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str) if not isinstance(data, str) else data
    return text if len(text) <= SHOW_CHARS else text[:SHOW_CHARS] + f"\n… ({len(text) - SHOW_CHARS} more characters)"


async def try_one(root: Path, text: str, suite: str, task_type: str) -> dict[str, Any]:
    """Run one public item through the draft and return every step's response."""
    from rightsize_core.integrity.dataset import load_suite_config, load_tasks, resolve_document
    from rightsize_core.systems.config import system_from_dict

    cfg = _cfg(text)
    system = system_from_dict(cfg, root)
    if hasattr(system, "trace"):
        system.trace = []
    task = next((t for t in load_tasks(root, suite, "public") if t.task_type == task_type), None)
    if task is None:
        raise ValueError(f"Suite {suite} has no public {task_type} items.")
    inputs = dict(task.inputs)
    if "document_ref" in inputs:
        inputs["document"] = resolve_document(root, load_suite_config(root, suite), task)
        inputs.pop("document_ref")

    # asyncio.timeout, not wait_for: an MCP connection must open and close in the same task.
    try:
        async with asyncio.timeout(TRY_TIMEOUT_S):
            await system.preflight()
            await system.setup()
            try:
                out = await system.run(task_type, inputs)
            finally:
                await system.teardown()
    finally:
        await system.aclose()
    steps = list(getattr(system, "trace", None) or [])
    trace_id = secrets.token_hex(6)
    raw = out.raw if isinstance(out.raw, dict) else {}
    result = {"task_id": task.id, "trace_id": trace_id, "query": inputs.get("query"),
              "steps": [{"step": s["step"], "shown": _show(s["response"])} for s in steps],
              "prediction": _show(out.prediction) if out.prediction is not None else None,
              "unmapped": [f"{k}={v!r}" for k, v in raw.get("unmapped", [])],
              "error": f"{out.error.kind}: {out.error.message}" if out.error else None}
    _TRACES[trace_id] = {"responses": [s["response"] for s in steps], "result": result}
    while len(_TRACES) > 20:
        _TRACES.popitem(last=False)
    return result


def jsonpath(trace_id: str, index: int, expr: str) -> dict[str, Any]:
    from rightsize_core.systems.declarative import jp_all

    responses = (_TRACES.get(trace_id) or {}).get("responses")
    if responses is None or not 0 <= index < len(responses):
        raise ValueError("That response is gone; try the item again.")
    try:
        matches = jp_all(expr, responses[index])
    except Exception as e:  # noqa: BLE001 - a malformed path is the answer the user needs
        return {"expr": expr, "index": index, "error": f"Not a JSONPath this parser accepts: {e}"}
    return {"expr": expr, "index": index, "count": len(matches), "shown": _show(matches[:20])}


async def conformance(root: Path, text: str, suite: str) -> dict[str, Any]:
    from rightsize_core.conformance.check import run_conformance
    from rightsize_core.systems.config import system_from_dict

    system = system_from_dict(_cfg(text), root)
    try:
        async with asyncio.timeout(CONFORMANCE_TIMEOUT_S):
            await system.preflight()
            report = await run_conformance(root, system, suite)
    finally:
        await system.aclose()
    return {"ok": report.ok, "summary": report.summary,
            "findings": [{"task_id": f.task_id, "ok": f.ok, "message": f.message,
                          "line": None if f.ok else locate(text, f.task_type, f.message)} for f in report.findings]}


def save(directory: Path, name: str, text: str, overwrite: bool) -> Path:
    if not NAME.match(name):
        raise ValueError("Name the config with lowercase letters, digits, - and _ (e.g. acme-search).")
    _cfg(text)
    dest = directory / f"{name}.yaml"
    if dest.exists() and not overwrite:
        raise ValueError(f"{dest.name} already exists; tick 'replace it' to overwrite.")
    directory.mkdir(parents=True, exist_ok=True)
    dest.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return dest


def claude_prompt(name: str, text: str, probs: list[dict], report: dict | None) -> str:
    issues = [f"- line {p['line']}: {p['message']}" for p in probs if p["severity"] == "error"]
    if report:
        issues += [f"- conformance ({f['task_id']}): {f['message']}" for f in report["findings"] if not f["ok"]]
    return (f"Use the rightsize plugin's benchmark-my-system skill to finish connecting the system "
            f"\"{name}\". Its draft config is below; its API docs are at: <link or file>.\n"
            + ("Problems so far:\n" + "\n".join(issues[:15]) + "\n" if issues else "")
            + "Get conformance passing, then plan a run and show me the plan before starting it.\n\n"
            + "```yaml\n" + text.rstrip() + "\n```\n")
