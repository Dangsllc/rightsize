"""Conformance: before scoring a system, prove its answers map onto the task contracts.

A mapping bug (wrong JSONPath, a status the config doesn't translate, citations buried in prose)
looks exactly like a weak model in the scores. Conformance runs a few public items and fails
loudly instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from rightsize_core.integrity.dataset import load_suite_config, load_tasks, resolve_document
from rightsize_core.packs import get_pack
from rightsize_core.systems.base import System

MIN_MAPPED_SHARE = 0.95


@dataclass
class Finding:
    task_id: str
    ok: bool
    message: str


@dataclass
class ConformanceReport:
    system: str
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, dict] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.findings) and all(f.ok for f in self.findings)

    def text(self) -> str:
        lines = [f"conformance · {self.system} · {'PASS' if self.ok else 'FAIL'}"]
        for tt, s in self.summary.items():
            lines.append(f"  {tt}: " + ", ".join(f"{k}={v}" for k, v in s.items()))
        for f in self.findings:
            lines.append(f"  [{'ok' if f.ok else 'FAIL'}] {f.task_id}: {f.message}")
        return "\n".join(lines)


async def run_conformance(root: Path, system: System, suite: str, per_type: int = 2) -> ConformanceReport:
    cfg = load_suite_config(root, suite)
    pack = get_pack(cfg["pack"])
    info = await system.info()
    report = ConformanceReport(system=info.name)
    tasks = load_tasks(root, suite, "public")
    for task_type in info.task_types:
        sample = [t for t in tasks if t.task_type == task_type][:per_type]
        if not sample:
            report.findings.append(Finding(task_type, False, "no public tasks of this type to test with"))
            continue
        spec = pack.task_types[task_type]
        shares, inferred = [], []
        await system.setup()
        try:
            for t in sample:
                inputs = dict(t.inputs)
                if "document_ref" in inputs:
                    inputs["document"] = resolve_document(root, cfg, t)
                    inputs.pop("document_ref")
                out = await system.run(task_type, inputs)
                if out.error:
                    report.findings.append(Finding(t.id, False, f"{out.error.kind}: {out.error.message[:300]}"))
                    continue
                try:
                    pred = spec.prediction_model.model_validate(out.prediction)
                except ValidationError as e:
                    report.findings.append(Finding(t.id, False, f"prediction does not match contract: {e.errors()[:2]}"))
                    continue
                unmapped = (out.raw or {}).get("unmapped") if isinstance(out.raw, dict) else None
                if unmapped:
                    vals = sorted({f"{k}={v!r}" for k, v in unmapped})
                    report.findings.append(Finding(t.id, False, f"values with no mapping: {', '.join(vals[:8])}"))
                    continue
                m = pack.conformance_metrics(t, pred) if hasattr(pack, "conformance_metrics") else {"mapped_share": 1.0}
                shares.append(m["mapped_share"])
                inferred.append(m.get("inferred_share", 0.0))
                ok = m["mapped_share"] >= MIN_MAPPED_SHARE
                msg = f"mapped {m['mapped_share']:.0%} of expected units ({m.get('items', '?')} items returned)"
                if m.get("extras"):
                    msg += f", {m['extras']} unrequested"
                if m.get("inferred_share"):
                    msg += f", {m['inferred_share']:.0%} citations inferred from free text"
                if not ok:
                    msg += f" — below {MIN_MAPPED_SHARE:.0%}; check the extract paths and code normalization"
                report.findings.append(Finding(t.id, ok, msg))
        finally:
            await system.teardown()
        if shares:
            report.summary[task_type] = {
                "mapped_share": round(sum(shares) / len(shares), 3),
                "inferred_share": round(sum(inferred) / len(inferred), 3),
                "checked": len(shares),
            }
    return report
