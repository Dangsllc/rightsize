"""A sweep's calibration.html: the cost-quality frontier and the 'cheapest model that's good
enough' tables, as the same self-contained page `rightsize export` publishes."""

from __future__ import annotations

from pathlib import Path


def render(result: dict, dest: Path) -> Path:
    """Write the calibration page for the sweep whose directory holds `dest` (calibration.json
    must already be written, as `rightsize calibrate` does)."""
    from rightsize_core.report.export import index_for, sweep_page

    sweep_dir = dest.parent
    dest.write_text(sweep_page(index_for(sweep_dir), sweep_dir.name), encoding="utf-8")
    return dest
