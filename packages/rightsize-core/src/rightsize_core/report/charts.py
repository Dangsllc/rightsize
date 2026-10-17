"""Inline SVG charts shared by the Workbench and exported reports. No scripts and no network:
each function returns markup that reads correctly on its own, with every label escaped.

* `interval`  one metric as an interval (the mark) with its point (a tick), floors on the same axis
* `frontier`  cost vs quality per rung: Pareto frontier, threshold line, the chosen rung emphasized
* `forest`    paired differences (B minus A) as a dot and its interval around a zero line

Color carries emphasis or status only, never identity alone: rung names are direct labels, and
status marks pair a shape with a word.
"""

from __future__ import annotations

import math
from html import escape


def _f(v: float) -> str:
    return f"{v:.2f}"


def interval(value: float | None, lo: float | None, hi: float | None, *, floors: dict[str, float] | None = None,
             low_n: bool = False, tiny_n: bool = False, metric: str = "", width: int = 560) -> str:
    """A 0-1 axis with the interval as a bar and the point as a ringed dot. Floors (trivial
    predictors) are ticks on the same axis, so 'better than nothing' is visible at a glance."""
    pad_l, pad_r, h = 8, 8, 58
    x0, x1 = 0.0, 1.0
    vals = [v for v in (value, lo, hi, *(floors or {}).values()) if v is not None]
    if vals and (min(vals) < 0 or max(vals) > 1):
        x0, x1 = min(0.0, min(vals)), max(1.0, max(vals))

    def X(v: float) -> float:
        return pad_l + (v - x0) / ((x1 - x0) or 1) * (width - pad_l - pad_r)

    axis_y = 30
    label = f"{escape(metric)} {_f(value)}" if value is not None else f"{escape(metric)} has no value"
    if value is not None and lo is not None and not tiny_n:
        label += f", 95% interval {_f(lo)} to {_f(hi)}"
    parts = [f'<svg class="chart interval" viewBox="0 0 {width} {h}" width="{width}" role="img" aria-label="{label}">']
    for i in range(5):
        v = x0 + (x1 - x0) * i / 4
        parts.append(f'<line class="grid" x1="{X(v):.1f}" y1="{axis_y - 14}" x2="{X(v):.1f}" y2="{axis_y + 6}"/>')
        parts.append(f'<text class="tick" x="{X(v):.1f}" y="{h - 4}" text-anchor="middle">{v:.2f}</text>')
    parts.append(f'<line class="axis" x1="{pad_l}" y1="{axis_y}" x2="{width - pad_r}" y2="{axis_y}"/>')
    placed: list[float] = []
    for name, f in sorted((floors or {}).items(), key=lambda kv: kv[1]):
        fx = X(f)
        parts.append(f'<line class="floor" x1="{fx:.1f}" y1="{axis_y - 9}" x2="{fx:.1f}" y2="{axis_y + 9}">'
                     f'<title>{escape(name)} (trivial predictor): {_f(f)}</title></line>')
        if all(abs(fx - p) > 80 for p in placed):
            parts.append(f'<text class="floor-label" x="{fx:.1f}" y="{axis_y + 19}" text-anchor="middle">'
                         f'{escape(name)} {_f(f)}</text>')
            placed.append(fx)
    if value is not None:
        if lo is not None and hi is not None and not tiny_n:
            cls = "ci low-n" if low_n else "ci"
            parts.append(f'<line class="{cls}" x1="{X(lo):.1f}" y1="{axis_y}" x2="{X(hi):.1f}" y2="{axis_y}">'
                         f'<title>95% interval {_f(lo)} to {_f(hi)}</title></line>')
        cls = "pt hollow" if tiny_n else "pt"
        parts.append(f'<circle class="{cls}" cx="{X(value):.1f}" cy="{axis_y}" r="5"><title>{label}</title></circle>')
        anchor = "end" if X(value) > width - 40 else "start" if X(value) < 40 else "middle"
        parts.append(f'<text class="val" x="{X(value):.1f}" y="{axis_y - 12}" text-anchor="{anchor}">{_f(value)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def frontier(points: list[dict], metric: str, threshold: float | None = None,
             width: int = 860, height: int = 360) -> str:
    """Cost per task (log scale) against the metric, one point per rung with its interval.

    The chosen rung is the emphasized mark; rungs on the Pareto frontier are joined by a thin line;
    dominated rungs recede. Points carry data attributes so the Workbench can re-decide in place.
    """
    pad_l, pad_r, pad_t, pad_b = 52, 120, 18, 46
    known = [p for p in points if p.get("cost") is not None]
    unknown = [p for p in points if p.get("cost") is None]
    if not known:
        names = ", ".join(escape(p["rung"]) for p in points)
        return (f'<p class="muted">No rung reported usage, so cost is unknown and there is no cost axis '
                f'({names}). The decision table below still applies, in ladder order.</p>')
    floor = min((p["cost"] for p in known if p["cost"] > 0), default=1e-4) / 3
    xs = [math.log10(max(p["cost"], floor)) for p in known]
    lx0, lx1 = min(xs) - 0.25, max(xs) + 0.25
    ys = [v for p in known for v in (p["lo"], p["hi"])] + ([threshold] if threshold is not None else [])
    y0, y1 = max(0.0, min(ys) - 0.03), min(1.0, max(ys) + 0.03)
    step = next(s for s in (0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5) if (y1 - y0) / s <= 6)
    y0, y1 = math.floor(y0 / step) * step, min(1.0, math.ceil(y1 / step) * step)
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    def X(c: float) -> float:
        return pad_l + (math.log10(max(c, floor)) - lx0) / ((lx1 - lx0) or 1) * plot_w

    def Y(v: float) -> float:
        return pad_t + (1 - (v - y0) / ((y1 - y0) or 1)) * plot_h

    parts = [(f'<svg class="chart frontier" viewBox="0 0 {width} {height}" width="{width}" role="img" '
              f'aria-label="Cost per task against {escape(metric)}, one point per rung" '
              f'data-y0="{y0}" data-y1="{y1}" data-top="{pad_t}" data-plot-h="{plot_h}">')]
    for i in range(round((y1 - y0) / step) + 1):
        v = y0 + i * step
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{Y(v):.1f}" x2="{width - pad_r}" y2="{Y(v):.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:.2f}</text>')
    for e in range(math.floor(lx0), math.ceil(lx1) + 1):
        if lx0 <= e <= lx1:
            x = pad_l + (e - lx0) / (lx1 - lx0) * plot_w
            money = f"${10 ** e:,.0f}" if e >= 0 else f"${10 ** e:.{-e}f}"
            parts.append(f'<text class="tick" x="{x:.1f}" y="{height - pad_b + 18}" text-anchor="middle">{money}</text>')
    parts.append(f'<line class="axis" x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" y2="{height - pad_b}"/>')
    parts.append(f'<text class="axis-title" x="{pad_l + plot_w / 2:.1f}" y="{height - 8}" text-anchor="middle">'
                 f'cost per task, log scale ($0 sits at the left edge)</text>')
    parts.append(f'<text class="axis-title" x="12" y="{pad_t + plot_h / 2:.1f}" text-anchor="middle" '
                 f'transform="rotate(-90 12 {pad_t + plot_h / 2:.1f})">{escape(metric)}</text>')
    ty = Y(threshold) if threshold is not None else -100
    hidden = "" if threshold is not None else ' style="display:none"'
    parts.append(f'<g class="threshold"{hidden}><line x1="{pad_l}" y1="{ty:.1f}" x2="{width - pad_r}" y2="{ty:.1f}"/>'
                 f'<text x="{width - pad_r + 6}" y="{ty + 4:.1f}">threshold '
                 f'<tspan class="threshold-value">{"" if threshold is None else _f(threshold)}</tspan></text></g>')
    front = sorted((p for p in known if p.get("frontier")), key=lambda p: p["cost"])
    if len(front) > 1:
        pts = " ".join(f"{X(p['cost']):.1f},{Y(p['value']):.1f}" for p in front)
        parts.append(f'<polyline class="frontier-line" points="{pts}"/>')
    placed: list[tuple[float, float]] = []
    for p in sorted(known, key=lambda p: (p["cost"], p["value"])):
        x, y = X(p["cost"]), Y(p["value"])
        cls = "rung" + (" chosen" if p.get("chosen") else "") + ("" if p.get("frontier") else " dominated")
        ly = y - 9
        while any(abs(ly - py) < 13 and abs(x - px) < 90 for px, py in placed):
            ly += 13
        placed.append((x, ly))
        tip = (f"{p['rung']}: {metric} {_f(p['value'])} (95% interval {_f(p['lo'])} to {_f(p['hi'])}), "
               f"${p['cost']:.4f} per task, n={p.get('n', '?')}")
        parts.append(
            f'<g class="{cls}" data-rung="{escape(p["rung"])}" data-lo="{p["lo"]}" tabindex="0">'
            f'<title>{escape(tip)}</title>'
            f'<line class="whisker" x1="{x:.1f}" y1="{Y(p["lo"]):.1f}" x2="{x:.1f}" y2="{Y(p["hi"]):.1f}"/>'
            f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="12"/>'
            f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="5"/>'
            f'<text class="rung-label" x="{x + 9:.1f}" y="{ly:.1f}">{escape(p["rung"])}</text></g>')
    parts.append("</svg>")
    if unknown:
        parts.append('<p class="muted small">Not on the chart, cost unknown: '
                     + ", ".join(escape(p["rung"]) for p in unknown) + ".</p>")
    return "".join(parts)


def forest(rows: list[dict], width: int = 860) -> str:
    """One row per metric: B minus A as a mark with its paired 95% interval, around zero.
    `flag` is regression | improved | "" and decides the mark's shape as well as its color."""
    if not rows:
        return ""
    pad_l, pad_r, row_h, top = 200, 210, 26, 10
    m = max(max(abs(r["lo"]), abs(r["hi"]), abs(r["value"])) for r in rows) or 0.01
    m *= 1.1
    h = top + row_h * len(rows) + 28

    def X(v: float) -> float:
        return pad_l + (v + m) / (2 * m) * (width - pad_l - pad_r)

    parts = [(f'<svg class="chart forest" viewBox="0 0 {width} {h}" width="{width}" role="img" '
              f'aria-label="Paired differences, B minus A, with 95% intervals">')]
    parts.append(f'<line class="zero" x1="{X(0):.1f}" y1="{top - 4}" x2="{X(0):.1f}" y2="{h - 24}"/>')
    for v in (-m / 1.1, 0.0, m / 1.1):
        parts.append(f'<text class="tick" x="{X(v):.1f}" y="{h - 8}" text-anchor="middle">{v:+.2f}</text>')
    for i, r in enumerate(rows):
        y = top + row_h * i + row_h / 2
        flag = r.get("flag") or ""
        name_cls = "metric primary" if r.get("primary") else "metric"
        parts.append(f'<text class="{name_cls}" x="{pad_l - 10}" y="{y + 4:.1f}" text-anchor="end">'
                     f'{escape(r["name"])}{"" if r.get("higher_is_better", True) else " ↓"}</text>')
        parts.append(f'<line class="ci {flag}" x1="{X(r["lo"]):.1f}" y1="{y:.1f}" x2="{X(r["hi"]):.1f}" y2="{y:.1f}"/>')
        cx = X(r["value"])
        if flag == "improved":
            mark = f'<path class="mk improved" d="M{cx:.1f},{y - 6:.1f} L{cx + 6:.1f},{y + 5:.1f} L{cx - 6:.1f},{y + 5:.1f} Z"/>'
        elif flag == "regression":
            mark = f'<path class="mk regression" d="M{cx:.1f},{y + 6:.1f} L{cx + 6:.1f},{y - 5:.1f} L{cx - 6:.1f},{y - 5:.1f} Z"/>'
        else:
            mark = f'<circle class="mk" cx="{cx:.1f}" cy="{y:.1f}" r="4.5"/>'
        parts.append(mark)
        word = {"improved": "▲ improved", "regression": "▼ regression"}.get(flag, "")
        parts.append(f'<text class="val" x="{width - pad_r + 10}" y="{y + 4:.1f}">{r["value"]:+.3f} '
                     f'[{r["lo"]:+.3f}, {r["hi"]:+.3f}]<tspan class="flag {flag}"> {word}</tspan></text>')
    parts.append("</svg>")
    return "".join(parts)
