"""Cost-vs-quality chart (inline SVG) and the 'cheapest model that's good enough' table."""

from __future__ import annotations

import math
from html import escape
from pathlib import Path


def _svg(points: list[dict], metric: str, w: int = 640, h: int = 320) -> str:
    pad_l, pad_r, pad_t, pad_b = 56, 16, 16, 44
    known = [p for p in points if p["cost_per_task"] is not None]
    if not known:
        return "<p class='sub'>No rung reported usage, so cost is unknown and there's no cost axis.</p>"
    floor = min((p["cost_per_task"] for p in known if p["cost_per_task"] > 0), default=1e-4) / 3
    xs = [math.log10(max(p["cost_per_task"], floor)) for p in known]
    x0, x1 = min(xs) - 0.2, max(xs) + 0.2
    ys = [v for p in known for v in (p["lo"], p["hi"])]
    y0, y1 = max(0.0, min(ys) - 0.05), min(1.0, max(ys) + 0.05)
    X = lambda c: pad_l + (math.log10(max(c, floor)) - x0) / (x1 - x0 or 1) * (w - pad_l - pad_r)
    Y = lambda v: pad_t + (1 - (v - y0) / (y1 - y0 or 1)) * (h - pad_t - pad_b)
    frontier, best = [], -1.0
    for p in sorted(known, key=lambda p: p["cost_per_task"]):
        if p["value"] > best:
            frontier.append(p)
            best = p["value"]
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="cost versus {escape(metric)}">',
             f'<line x1="{pad_l}" y1="{h - pad_b}" x2="{w - pad_r}" y2="{h - pad_b}" class="ax"/>',
             f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{h - pad_b}" class="ax"/>']
    for i in range(5):
        v = y0 + (y1 - y0) * i / 4
        parts.append(f'<text x="{pad_l - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="tk">{v:.2f}</text>')
    for e in range(math.floor(x0), math.ceil(x1) + 1):
        if x0 <= e <= x1:
            x = pad_l + (e - x0) / (x1 - x0) * (w - pad_l - pad_r)
            parts.append(f'<text x="{x:.1f}" y="{h - pad_b + 16}" text-anchor="middle" class="tk">${10 ** e:g}</text>')
    parts.append(f'<text x="{(w + pad_l) / 2}" y="{h - 6}" text-anchor="middle" class="tk">cost per task (log scale; $0 shown at the left edge)</text>')
    if len(frontier) > 1:
        pts = " ".join(f"{X(p['cost_per_task']):.1f},{Y(p['value']):.1f}" for p in frontier)
        parts.append(f'<polyline points="{pts}" class="fr"/>')
    for p in known:
        x = X(p["cost_per_task"])
        parts.append(f'<line x1="{x:.1f}" y1="{Y(p["lo"]):.1f}" x2="{x:.1f}" y2="{Y(p["hi"]):.1f}" class="ci"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{Y(p["value"]):.1f}" r="4" class="pt"><title>{escape(p["rung"])}: {p["value"]:.3f}</title></circle>')
        parts.append(f'<text x="{x + 6:.1f}" y="{Y(p["value"]) - 6:.1f}" class="lb">{escape(p["rung"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render(result: dict, dest: Path) -> Path:
    css = """:root{--bg:#fbfaf8;--fg:#1d1c1a;--muted:#6b6862;--line:#e4e1db;--card:#fff;--accent:#2f5d8a;--ci:#9db6cf}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecebe8;--muted:#9a978f;--line:#2e2d2a;--card:#1e1e1c;--accent:#8db4dc;--ci:#3e5770}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}main{max-width:1040px;margin:0 auto;padding:28px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted)}
.sub{color:var(--muted)}.ax{stroke:var(--line)}.tk,.lb{fill:var(--muted);font-size:11px}.ci{stroke:var(--ci);stroke-width:3}.pt{fill:var(--accent)}.fr{fill:none;stroke:var(--accent);stroke-dasharray:4 3}"""
    rows = []
    for d in result["decisions"]:
        warn = " <span class='sub'>(fewer than 30 clusters)</span>" if d["insufficient_n"] else ""
        rows.append(f"<tr><td>{escape(d['task_type'])}</td><td>{escape(d['stratum'])}{warn}</td>"
                    f"<td><b>{escape(d['chosen'] or '—')}</b></td><td>{escape(d['best'])}</td>"
                    f"<td class='sub'>{escape(d['why'])}</td></tr>")
    charts = []
    for tt in sorted({p["task_type"] for p in result["points"]}):
        pts = [p for p in result["points"] if p["task_type"] == tt]
        charts.append(f"<h2>{escape(tt)} · {escape(pts[0]['metric'])}</h2><div class='card'>{_svg(pts, pts[0]['metric'])}</div>")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model right-sizing</title><style>{css}</style></head><body><main>
<h1>Cheapest model that's good enough</h1>
<p class="sub">sweep {escape(result['sweep_id'])} · thresholds {escape(str(result['thresholds']))} · non-inferiority margin {result['delta']}</p>
<div class="card"><table><tr><th>task</th><th>tier</th><th>cheapest good-enough</th><th>best scoring</th><th>why</th></tr>{''.join(rows)}</table></div>
{''.join(charts)}
</main></body></html>"""
    dest.write_text(html, encoding="utf-8")
    return dest
