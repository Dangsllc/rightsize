"""A run's own scorecard.html: the same page `rightsize export` publishes and the Workbench shows,
as one self-contained file (no scripts, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def render(card: dict[str, Any], dest: Path) -> Path:
    """Write the scorecard page for the run whose directory holds `dest` (scorecard.json must
    already be written, as `score_run` does)."""
    from rightsize_core.report.export import index_for, run_page

    run_dir = dest.parent
    dest.write_text(run_page(index_for(run_dir), run_dir.name), encoding="utf-8")
    return dest
