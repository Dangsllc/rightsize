// Rightsize Workbench. Progressive enhancement only: every page reads and works without this file.
// Dynamic text always goes in through textContent; nothing from a run is ever parsed as HTML.
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

// ---- theme (a per-viewer convenience; storage may be unavailable)
(function theme() {
  const sel = $("[data-theme-select]");
  let saved = "";
  try { saved = localStorage.getItem("rightsize-theme") || ""; } catch (e) { saved = ""; }
  const apply = (v) => { if (v) document.documentElement.setAttribute("data-theme", v); else document.documentElement.removeAttribute("data-theme"); };
  apply(saved);
  if (!sel) return;
  sel.value = saved;
  sel.addEventListener("change", () => { apply(sel.value); try { localStorage.setItem("rightsize-theme", sel.value); } catch (e) { /* private window */ } });
})();

// ---- copy buttons
$$("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
  const src = btn.parentElement.querySelector("[data-copy-source]");
  if (!src) return;
  try { await navigator.clipboard.writeText(src.textContent.trim()); btn.textContent = "Copied"; }
  catch (e) { btn.textContent = "Select and copy"; }
  setTimeout(() => { btn.textContent = "Copy"; }, 1600);
}));

// ---- small form behaviours
$$("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form.submit()));
$$(".js-hide").forEach((el) => { el.hidden = true; });
$$("[data-refresh]").forEach((el) => el.addEventListener("change", () => {
  const f = el.form;
  const a = document.createElement("input");
  a.type = "hidden"; a.name = "action"; a.value = "refresh";
  f.appendChild(a); f.submit();
}));
$$("form[data-two-step]").forEach((f) => f.addEventListener("submit", (ev) => {
  const btn = $("button", f);
  if (btn.classList.contains("armed")) return;
  ev.preventDefault();
  btn.classList.add("armed");
  btn.dataset.label = btn.textContent;
  btn.textContent = btn.dataset.armLabel || "Click again";
  setTimeout(() => { btn.classList.remove("armed"); btn.textContent = btn.dataset.label; }, 4000);
}));
(function compare() {
  const f = $("form[data-compare]");
  if (!f) return;
  const btn = $("[data-compare-button]", f);
  const update = () => { btn.disabled = $$("input[name=pick]:checked", f).length !== 2; };
  f.addEventListener("change", update); update();
})();

// ---- the config editor: jump to a problem's line; start over from a template
$$("[data-goto-line]").forEach((a) => a.addEventListener("click", (ev) => {
  const ta = $("[data-editor]");
  if (!ta) return;
  ev.preventDefault();
  const line = parseInt(a.dataset.gotoLine, 10);
  const lines = ta.value.split("\n");
  const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
  ta.focus();
  ta.setSelectionRange(start, start + (lines[line - 1] || "").length);
  ta.scrollTop = Math.max(0, (line - 5) * parseFloat(getComputedStyle(ta).lineHeight || "19"));
}));
(function templateJump() {
  const sel = $("[data-template-jump]");
  if (!sel) return;
  sel.addEventListener("change", () => {
    if (sel.value) location.href = sel.value === "blank" ? "/systems/new" : `/systems/new?template=${encodeURIComponent(sel.value)}`;
  });
})();

// ---- tooltips for chart marks (hover and keyboard focus show the same text)
(function tips() {
  const marks = $$(".chart .rung, .chart .pt, .chart .floor");
  if (!marks.length) return;
  const tip = document.createElement("div");
  tip.className = "tip"; tip.hidden = true; tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);
  marks.forEach((m) => {
    const t = m.querySelector("title");
    if (!t) return;
    m.dataset.tip = t.textContent; m.setAttribute("aria-label", t.textContent); t.remove();
    const show = (x, y) => { tip.textContent = m.dataset.tip; tip.hidden = false; tip.style.left = `${Math.min(x + 14, innerWidth - 330)}px`; tip.style.top = `${y + 14}px`; };
    m.addEventListener("pointermove", (e) => show(e.clientX, e.clientY));
    m.addEventListener("pointerleave", () => { tip.hidden = true; });
    m.addEventListener("focus", () => { const r = m.getBoundingClientRect(); show(r.right, r.top); });
    m.addEventListener("blur", () => { tip.hidden = true; });
  });
})();

// ---- live jobs and runs: poll, update in place, reload once the state changes
async function poll(url, every, onData) {
  let stop = false;
  while (!stop) {
    await new Promise((r) => setTimeout(r, every));
    try {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) continue;
      stop = onData(await res.json()) === false;
    } catch (e) { /* server restarting; keep the frame and try again */ }
  }
}
(function liveJob() {
  const head = $("[data-job]");
  if (!head || head.dataset.state !== "running") return;
  poll(`/api/jobs/${encodeURIComponent(head.dataset.job)}`, 1500, (d) => {
    const runsNow = d.runs.map((r) => r.run_id).join(",");
    const runsShown = $$("[data-live-run]").map((el) => el.dataset.liveRun).join(",");
    if (d.job.state !== "running" || runsNow !== runsShown) { location.reload(); return false; }
    d.runs.forEach((r) => {
      const el = $(`[data-live-run="${CSS.escape(r.run_id)}"]`);
      if (!el) return;
      const bar = $(".meter i", el), text = $(".num", el), p = r.progress;
      if (bar) bar.style.width = `${(100 * p.done / (p.total || 1)).toFixed(1)}%`;
      if (text) text.textContent = `${p.done} of ${p.total} calls · ${p.errors} failed · reported spend $${p.spend_usd.toFixed(4)}`;
    });
    const log = $("[data-log]");
    if (log) log.textContent = d.job.log_tail.join("\n");
    return true;
  });
})();
(function liveRun() {
  const head = $("[data-run]");
  if (!head || head.dataset.health !== "running") return;
  poll(`/api/runs/${encodeURIComponent(head.dataset.run)}`, 2000, (s) => {
    if (s.health !== "running") { location.reload(); return false; }
    const bar = $("[data-progress] i"), text = $("[data-progress-text]"), p = s.progress;
    if (bar) bar.style.width = `${(100 * p.done / (p.total || 1)).toFixed(1)}%`;
    if (text) text.textContent = `${p.done} of ${p.total} calls · ${p.errors} failed · reported spend $${p.spend_usd.toFixed(4)}`;
    return true;
  });
})();

// ---- calibration what-if (the rule itself lives in decide.js)
const money = (v) => (v === null || v === undefined ? "unknown" : v === 0 ? "$0 · local" : v < 0.01 ? `$${v.toFixed(4)}` : v < 1 ? `$${v.toFixed(3)}` : `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
(function whatif() {
  const data = $("#calibration-data");
  const form = $("form[data-whatif]");
  if (!data || !form) return;
  const cal = JSON.parse(data.textContent);
  const saved = Object.fromEntries(cal.decisions.map((d) => [`${d.task_type}|${d.stratum}`, d.chosen]));
  const chosenNow = {};

  function settings() {
    const tau = {};
    $$("[data-threshold]", form).forEach((el) => {
      const off = $(`[data-threshold-off="${el.id}"]`, form);
      tau[el.dataset.tt] = off && off.checked ? null : parseFloat(el.value);
      const out = $(`[data-out="${el.id}"]`, form);
      if (out) out.textContent = tau[el.dataset.tt] === null ? "off" : tau[el.dataset.tt].toFixed(2);
    });
    const delta = parseFloat($("[data-delta]", form).value);
    $("[data-out=delta]", form).textContent = delta.toFixed(3);
    const fm = $("[data-floor-metric]", form).value, fv = parseFloat($("[data-floor-value]", form).value);
    const floors = fm && !Number.isNaN(fv) ? { [fm]: fv } : {};
    return { tau, delta, floors };
  }

  function moveThreshold(tt, tau) {
    const svg = $(`[data-frontier="${CSS.escape(tt)}"] svg`);
    if (!svg) return;
    const g = $(".threshold", svg);
    if (tau === null) { g.style.display = "none"; return; }
    const y0 = +svg.dataset.y0, y1 = +svg.dataset.y1, top = +svg.dataset.top, h = +svg.dataset.plotH;
    const clamped = Math.max(y0, Math.min(y1, tau));
    const y = top + (1 - (clamped - y0) / ((y1 - y0) || 1)) * h;
    g.style.display = "";
    $("line", g).setAttribute("y1", y); $("line", g).setAttribute("y2", y);
    $("text", g).setAttribute("y", y + 4);
    $(".threshold-value", g).textContent = tau.toFixed(2) + (tau !== clamped ? (tau > clamped ? " (above)" : " (below)") : "");
  }

  function redraw() {
    const s = settings();
    let changed = 0;
    cal.decisions.forEach((d) => {
      const tau = s.tau[d.task_type] ?? null;
      const [chosen, why] = decide(d.rungs, d.best, tau, s.delta, s.floors);
      const row = $(`table[data-decisions="${CSS.escape(d.task_type)}"] tr[data-stratum="${CSS.escape(d.stratum)}"]`);
      if (row) {
        $("[data-chosen] b", row).textContent = chosen || "no rung qualifies";
        $("[data-why]", row).textContent = why || "no rung qualifies";
        const diff = chosen !== saved[`${d.task_type}|${d.stratum}`];
        row.classList.toggle("changed", diff);
        if (diff) changed += 1;
      }
      if (d.stratum === "all") {
        chosenNow[d.task_type] = chosen;
        $$(`[data-frontier="${CSS.escape(d.task_type)}"] .rung`).forEach((g) => g.classList.toggle("chosen", g.dataset.rung === chosen));
        moveThreshold(d.task_type, tau);
      }
    });
    const note = $("[data-changed-note]");
    if (note) note.hidden = changed === 0;
    volume();
  }

  function volume() {
    const input = $("[data-volume-input]");
    if (!input) return;
    const v = Math.max(0, parseFloat(input.value) || 0);
    cal.decisions.filter((d) => d.stratum === "all").forEach((d) => {
      const row = $(`[data-volume-row="${CSS.escape(d.task_type)}"]`);
      if (!row) return;
      const chosen = chosenNow[d.task_type] !== undefined ? chosenNow[d.task_type] : d.chosen;
      const ch = chosen ? d.rungs[chosen] : null, be = d.rungs[d.best];
      $("[data-route]", row).textContent = chosen || "—";
      $("[data-unit]", row).textContent = ch ? money(ch.cost) : "—";
      $("[data-total]", row).textContent = ch && ch.cost !== null ? money(ch.cost * v) : "unknown";
      $("[data-vs]", row).textContent = chosen === d.best ? "same rung" : ch && be && ch.cost !== null && be.cost !== null
        ? `${d.best}: ${money(be.cost * v)}${be.cost > ch.cost ? ` (saves ${money((be.cost - ch.cost) * v)})` : ""}` : "—";
    });
  }

  form.addEventListener("input", redraw);
  form.addEventListener("change", redraw);
  const vi = $("[data-volume-input]");
  if (vi) vi.addEventListener("input", volume);
  redraw();
})();
