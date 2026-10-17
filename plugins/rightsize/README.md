# Rightsize for Claude Code

Benchmark a compliance AI system and find the cheapest model that's good enough for each task,
from Claude Code. The plugin starts Rightsize's MCP server from a local checkout and adds two
skills and a setup command.

## Install
```
/plugin marketplace add Dangsllc/rightsize
/plugin install rightsize@rightsize
/rightsize:setup
```
The server runs from a Rightsize checkout: `$RIGHTSIZE_ROOT`, else `~/rightsize` (setup offers to
clone it there). API keys go in that checkout's `.env`; the tools only check that they're set.

## What's inside
- **Skills:** `benchmark-my-system` (write a config from a system's API docs, prove it with
  conformance, run with a cap, report with caveats) and `right-size-models` (pilot, sweep,
  calibrate, explain routing.yaml).
- **Tools** (`rightsize` MCP server): `list_runs`, `summarize_run`, `list_items`, `get_item`,
  `diff_runs`, `list_sweeps`, `get_calibration`, `preview_routing`, `list_jobs`, `job_status`,
  `check_conformance`, `plan_run`, `plan_sweep`, `start`, `calibrate`, `cancel_job`.
- **Command:** `/rightsize:setup`.

Nothing that calls a model starts without a plan: `plan_run` or `plan_sweep` returns the calls, a
cost estimate measured from earlier runs, and every guard (held-out data, spend cap, keys), and
`start(plan_id)` refuses if anything changed since. Jobs started here appear in the Workbench
(`uv run rightsize ui` in the checkout), where a person can follow or cancel them.
