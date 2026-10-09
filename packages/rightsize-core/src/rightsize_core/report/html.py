"""Render scorecard.json as a single self-contained HTML page (no scripts, no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rightsize scorecard</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1c1a;--muted:#6b6862;--line:#e4e1db;--card:#fff;--accent:#2f5d8a;--good:#2e7d4f;--warn:#a86412;--bad:#a63d3d;--bar:#d7e3ef}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecebe8;--muted:#9a978f;--line:#2e2d2a;--card:#1e1e1c;--accent:#8db4dc;--good:#6cc08f;--warn:#e0a458;--bad:#e27d7d;--bar:#2a3a4b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}h3{font-size:14px;margin:18px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);font-size:13px}.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid currentColor}
.ok{color:var(--good)}.degraded{color:var(--warn)}.aborted{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:12px 0}
.hero{display:flex;gap:28px;flex-wrap:wrap;align-items:flex-end}.big{font-size:34px;font-weight:650;font-variant-numeric:tabular-nums}
.ci{color:var(--muted);font-size:13px}table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}td.n{text-align:right}
.bar{position:relative;height:8px;background:var(--line);border-radius:4px;min-width:120px}.bar i{position:absolute;top:0;bottom:0;background:var(--bar);border-radius:4px}.bar b{position:absolute;top:-3px;width:2px;height:14px;background:var(--accent)}
.wrap{overflow-x:auto}code{font-size:12.5px}
</style></head><body><main>
<h1>{{ c.system }} <span class="sub">· {{ c.model or "default model" }}</span></h1>
<div class="sub">run <code>{{ c.run_id }}</code> · split {{ c.split }} · repeats {{ c.repeats }} ·
<span class="pill {{ c.status }}">{{ c.status }}</span></div>
{% for tt, r in c.task_types.items() %}
<h2>{{ tt }}</h2>
<div class="card hero">
  {% set pm = r.metrics.get(r.primary_metric) %}
  <div><div class="sub">{{ r.primary_metric }}</div>
  <div class="big">{{ fmt(pm.value) if pm else "n/a" }}</div>
  {% if pm %}<div class="ci">95% CI {{ fmt(pm.lo) }} – {{ fmt(pm.hi) }} · {{ pm.n_clusters }} clusters</div>{% endif %}</div>
  {% if r.usage.cost_usd_per_task is not none %}<div><div class="sub">cost / task</div><div class="big">${{ "%.4f"|format(r.usage.cost_usd_per_task) }}</div></div>{% else %}<div><div class="sub">cost</div><div class="big">unknown</div><div class="ci">system reported no usage</div></div>{% endif %}
  {% if r.usage.latency_ms_p50 is not none %}<div><div class="sub">latency p50 / p95 ({{ r.usage.latency_kind }})</div><div class="big">{{ ms(r.usage.latency_ms_p50) }}</div><div class="ci">p95 {{ ms(r.usage.latency_ms_p95) }}</div></div>{% endif %}
  {% if r.stability %}<div><div class="sub">pass^{{ r.stability.k|int }} / flip rate</div><div class="big">{{ fmt(r.stability["pass^k"]) }}</div><div class="ci">flip {{ fmt(r.stability.flip_rate) }}</div></div>{% endif %}
</div>
{% if r.floors %}<h3>Against trivial predictors</h3><div class="card wrap"><table><tr><th>predictor</th><th class="n">{{ r.primary_metric }}</th></tr>
<tr><td><b>this run</b></td><td class="n"><b>{{ fmt(pm.value) if pm else "n/a" }}</b></td></tr>
{% for name, f in r.floors.items() %}<tr><td>{{ name }}</td><td class="n">{{ fmt(f.get(r.primary_metric)) }}</td></tr>{% endfor %}</table></div>{% endif %}
<h3>Metrics</h3><div class="card wrap"><table><tr><th>metric</th><th class="n">value</th><th class="n">95% CI</th><th>interval</th></tr>
{% for name, m in r.metrics.items() %}<tr><td>{{ name }}{% if r.higher_is_better.get(name) == false %} <span class="sub">(lower is better)</span>{% endif %}</td><td class="n">{{ fmt(m.value) }}</td><td class="n">{{ fmt(m.lo) }} – {{ fmt(m.hi) }}</td>
<td>{% if 0 <= m.lo and m.hi <= 1 %}<div class="bar"><i style="left:{{ pct(m.lo) }}%;width:{{ pct(m.hi - m.lo) }}%"></i><b style="left:{{ pct(m.value) }}%"></b></div>{% endif %}</td></tr>{% endfor %}</table></div>
{% if r.strata %}<h3>By difficulty tier</h3><div class="card wrap"><table><tr><th>tier</th>{% for k in strata_cols(r) %}<th class="n">{{ k }}</th>{% endfor %}</tr>
{% for s, vals in r.strata.items() %}<tr><td>{{ s }}</td>{% for k in strata_cols(r) %}<td class="n">{{ fmt(vals.get(k)) }}</td>{% endfor %}</tr>{% endfor %}</table></div>{% endif %}
{% endfor %}
<h2>Manifest</h2><div class="card wrap"><pre style="margin:0;font-size:12px;white-space:pre-wrap">{{ manifest_json }}</pre></div>
</main></body></html>"""


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer() and abs(v) >= 2:
        return str(int(v))
    return f"{v:.3f}" if isinstance(v, float | int) else str(v)


def _ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms"


def _strata_cols(r: dict) -> list[str]:
    cols: list[str] = []
    for vals in r["strata"].values():
        for k in vals:
            if k not in cols:
                cols.append(k)
    return cols


def render(card: dict[str, Any], dest: Path) -> Path:
    env = Environment(autoescape=select_autoescape())
    tpl = env.from_string(TEMPLATE)
    html = tpl.render(
        c=card, fmt=_fmt, ms=_ms, strata_cols=_strata_cols,
        pct=lambda x: max(0.0, min(100.0, 100 * float(x))),
        manifest_json=json.dumps(card.get("manifest", {}), indent=2),
    )
    dest.write_text(html, encoding="utf-8")
    return dest
