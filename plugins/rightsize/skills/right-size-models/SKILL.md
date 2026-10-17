---
name: right-size-models
description: >
  Find the cheapest model that's good enough for each compliance task with Rightsize: run a pilot
  to measure cost, sweep a ladder of models with a spend cap, calibrate against thresholds the
  user chooses, and explain the routing with its evidence and limits. Use when the user asks
  "which model do we actually need", "can a smaller model do this", "right-size our models",
  "cut our AI spend", "compare local and hosted models", "run a sweep", "calibrate", or asks about
  routing.yaml.
---

# Right-size models with Rightsize

A sweep runs the same tasks across a ladder of models, cheapest first. Calibration then picks,
per task type and tier, the cheapest rung that's good enough, and writes `routing.yaml`. The
`rightsize` MCP tools do the work; if they're missing, run `/rightsize:setup`.

## Rules that hold throughout
- **Spend only with consent.** Every sweep is planned (`plan_sweep`), shown to the user, and
  started with `start(plan_id)` only after they agree. Paid rungs need a spend cap.
- **Keys stay out of the conversation.** A missing key shows up in the plan as `missing_env`;
  tell the user which variable to add to the checkout's `.env`.
- **"Good enough" is the user's call.** Propose thresholds; don't pick them silently.
- **Evidence over verdicts.** A route on fewer than 30 clusters is marked `insufficient_n` and is
  not a finding. Say so whenever you mention it.

## 1. Choose a ladder
The checkout ships `configs/ladders/default.yaml` (Claude models) and
`configs/ladders/local-vs-api.yaml` (local models through LM Studio and Ollama, then Gemini).
To build one, see `references/ladders-and-thresholds.md`. Every rung needs a price in
`configs/pricing/`, or its cost is unknown and calibration falls back to ladder order.
The system must take a model parameter; `ref:llm` does, and so does any config with
`model_param`.

## 2. Pilot to measure cost
`plan_sweep(system="ref:llm", ladder=..., suite="m1", split="dev", sample=20, repeats=1,
max_cost=5)`. Show the plan; if it's blocked, the reasons say what to fix. On agreement,
`start(plan_id)`, then `job_status(job_id)` about once a minute (the Workbench shows it live:
`uv run rightsize ui`). Rungs whose first calls all fail stop early; report their hint.

## 3. The full sweep
Plan again at full size on the split you'll report: `split="public"`, `repeats=3`, no `sample`.
The estimate now comes from the pilot's measured cost per task. Propose a cap with headroom over
it (e.g. 1.5x) and show the user the arithmetic. On agreement, start and follow it.

## 4. Decide what "good enough" means
Ask the user. As a starting point: classification `group.macro_f1` 0.70, retrieval `ndcg@10`
0.60, and a margin of 0.03 against the best rung. A floor can protect what matters most, such as
`spec.deficiency_recall` 0.9 (missing a gap is worse than a false alarm). Before saving,
`preview_routing(sweep_id, thresholds, delta, floors)` shows instantly how routes move with the
choice; show the user one or two alternatives.

## 5. Calibrate and explain
`calibrate(sweep_id, thresholds, delta, floors)` runs as a job; then `get_calibration(sweep_id)`.
For each task type report the chosen rung, `why`, its lower bound and cost per task, the best
rung for contrast, and any `insufficient_n`. Tier decisions (e.g. a cheaper model for
`query_style=exact_term`) only become overrides in `routing.yaml` with 30 or more clusters.

## 6. What it costs at their volume
Measured cost per task × the user's monthly volume, for the chosen route and for always using
the best rung. Nothing more: no token model, no forecast. A rung priced at $0 is free at the
margin, not free to run.

## Caveats to state
- Classification items in both suites were written by Claude models, so a Claude rung's
  classification score may carry writer bias; retrieval routing is the cleaner comparison.
- Results describe compliance work on HIPAA policies and BAAs, not tasks in general.
- Publishing: `rightsize export` (in the checkout) writes a static bundle from public and dev
  runs only; it refuses held-out data.
