"""One way to render Rightsize numbers, shared by the Workbench and every exported report.

The Workbench adds its app shell and controls on top; exports add a static shell. Both draw the
same macros, tables, and charts from `report/templates/`, so a number reads the same in the
browser, in a published report, and in a run's own scorecard.html.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from rightsize_core.report import charts

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

# glyph, label, tone. Tone picks a color; the glyph and the label carry the meaning.
HEALTH = {
    "running": ("◐", "Running", "info"),
    "interrupted": ("◌", "Interrupted", "warning"),
    "no_signal": ("✕", "No signal", "critical"),
    "aborted": ("■", "Stopped early", "warning"),
    "degraded": ("▲", "Degraded", "warning"),
    "ok": ("●", "OK", "good"),
    "unscored": ("○", "Not scored", "info"),
}
SWEEP = {
    "empty": ("✕", "Died before first rung", "critical"),
    "running": ("◐", "Running", "info"),
    "interrupted": ("◌", "Interrupted", "warning"),
    "failed": ("✕", "Failed", "critical"),
    "cancelled": ("■", "Cancelled", "warning"),
    "stopped": ("■", "Stopped at the cap", "warning"),
    "complete": ("●", "Complete", "good"),
}
JOB = {
    "running": ("◐", "Running", "info"),
    "finished": ("●", "Finished", "good"),
    "failed": ("✕", "Failed", "critical"),
    "cancelled": ("■", "Cancelled", "warning"),
    "lost": ("◌", "Lost", "warning"),
}
RUNG = {"pending": ("○", "Pending", "info"), "running": ("◐", "Running", "info"), "done": ("●", "Done", "good"),
        "scoring": ("◐", "Scoring", "info"), "failed": ("✕", "Failed", "critical"),
        "cancelled": ("■", "Cancelled", "warning")}
TONE_ORDER = {"critical": 0, "warning": 1, "info": 2, "good": 3}


def fmt(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float | int):
        return f"{v:.{digits}f}"
    return str(v)


def money(v: float | None, state: str | None = None) -> Markup:
    """Unknown cost, a $0 local model, and a measured cost are three different facts."""
    if v is None or state == "unknown":
        return Markup('<span class="cost unknown" title="The system reported no usage, so cost is unknown">'
                      'unknown</span>')
    if v == 0 or state == "zero":
        return Markup('<span class="cost zero" title="Priced at $0, e.g. a local model">$0 · local</span>')
    text = f"${v:.4f}" if v < 0.01 else f"${v:.3f}" if v < 1 else f"${v:,.2f}"
    return Markup('<span class="cost">') + text + Markup("</span>")


def ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms"


def when(iso: str | None) -> str:
    """'2026-10-14 17:05' from an ISO timestamp."""
    return "—" if not iso else iso.replace("T", " ")[:16]


def segments(text: str, highlights: list) -> list[tuple[str, Any]]:
    """Split a document into plain and highlighted runs. Overlapping highlights keep the first."""
    out, pos = [], 0
    for h in sorted(highlights, key=lambda h: h.start):
        if h.start < pos or h.end <= h.start:
            continue
        if h.start > pos:
            out.append((text[pos:h.start], None))
        out.append((text[h.start:h.end], h))
        pos = h.end
    out.append((text[pos:], None))
    return out


def explain(e: Exception) -> str:
    """One readable sentence for why a run can't be read, instead of a validation dump."""
    text = str(e)
    if "rightsize_version" in text and "Field required" in text:
        return ("it was written by an older version of Rightsize, before manifests recorded "
                "rightsize_version. Its saved scorecard still shows on the run page.")
    return text.strip().splitlines()[0][:300] if text.strip() else type(e).__name__


def qs(**kw: Any) -> str:
    return urlencode({k: v for k, v in kw.items() if v not in (None, "", [])}, doseq=True)


def stylesheet(*names: str) -> str:
    """The shared CSS files, concatenated for inlining into a self-contained page."""
    return "\n".join((STATIC / n).read_text() for n in (names or ("tokens.css", "report.css")))


def environment(*extra_dirs: Path) -> Environment:
    """A Jinja environment over `extra_dirs` (first match wins) and the shared report templates."""
    env = Environment(loader=ChoiceLoader([FileSystemLoader([*extra_dirs, TEMPLATES])]),
                      autoescape=select_autoescape(default=True, default_for_string=True))
    env.globals.update(
        fmt=fmt, money=money, ms=ms, when=when, segments=segments, qs=qs,
        HEALTH=HEALTH, SWEEP=SWEEP, JOB=JOB, RUNG=RUNG,
        interval=lambda *a, **k: Markup(charts.interval(*a, **k)),
        frontier=lambda *a, **k: Markup(charts.frontier(*a, **k)),
        forest=lambda *a, **k: Markup(charts.forest(*a, **k)),
    )
    return env
