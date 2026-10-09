from __future__ import annotations

from pathlib import Path

from rightsize_core.integrity.dataset import load_suite_config

from rightsize_reference_systems.base import ReferenceSystem


def make(name: str, root: Path, suite: str = "m1", **kwargs) -> ReferenceSystem:
    cfg = load_suite_config(root, suite)
    sources = [root / s for s in cfg["sources"]]
    if name == "bm25":
        from rightsize_reference_systems.bm25 import BM25System

        return BM25System(sources)
    if name == "oracle":
        from rightsize_reference_systems.oracle import OracleSystem

        return OracleSystem(root, suite, kwargs.get("split", "public"))
    if name == "llm":
        from rightsize_reference_systems.llm import LLMSystem

        return LLMSystem(root, sources, **kwargs)
    if name == "dense":
        from rightsize_reference_systems.dense import DenseSystem

        return DenseSystem(sources)
    raise KeyError(f"Unknown reference system {name!r} (bm25, dense, llm, oracle)")
