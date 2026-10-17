// The routing rule, mirrored from rightsize_core.calibration.sweep.decide so the Workbench can
// re-decide in the browser. tests/ui/test_whatif_parity.py runs this file under node against the
// Python rule; change both together.
"use strict";

const MAX_ERROR = 0.02;
function rungOrder(rungs) {
  const names = Object.keys(rungs);
  if (names.every((n) => rungs[n].cost !== null)) {
    return names.sort((a, b) => (rungs[a].cost - rungs[b].cost) || (rungs[a].ladder_index - rungs[b].ladder_index));
  }
  return names.sort((a, b) => rungs[a].ladder_index - rungs[b].ladder_index);
}
function floorOk(r, floors) {
  return Object.entries(floors || {}).every(([m, v]) => !(m in r.metrics) || r.metrics[m] >= v);
}
const signed = (v) => `${v >= 0 ? "+" : ""}${v.toFixed(3)}`;
function decide(rungs, best, tau, delta, floors) {
  for (const name of rungOrder(rungs)) {
    const e = rungs[name];
    if (e.error_rate > MAX_ERROR || !floorOk(e, floors)) continue;
    if (tau !== null && e.lo >= tau) return [name, `lower bound ${e.lo.toFixed(3)} >= ${tau}`];
    if (name === best) return [name, "best rung"];
    if (e.diff_lo !== null && e.diff_lo >= -delta) return [name, `within ${delta} of best (${best}): diff lower bound ${signed(e.diff_lo)}`];
  }
  return [null, null];
}
if (typeof module !== "undefined") module.exports = { decide, rungOrder, floorOk };
