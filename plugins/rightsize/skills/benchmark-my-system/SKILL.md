---
name: benchmark-my-system
description: >
  Benchmark a compliance AI system (the user's own product, a vendor's, or a model behind an API)
  with Rightsize: connect it through a config written from its API docs, prove the mapping with
  conformance, run the HIPAA and BAA suites with a spend cap, and report results with their
  intervals, errors, cost, and caveats intact. Use when the user says "benchmark my system",
  "how good is our compliance AI", "test our API against Rightsize", "evaluate this vendor",
  "write a Rightsize config", "why did it score so low", or names a system they want measured.
---

# Benchmark a system with Rightsize

The `rightsize` MCP tools do the work; the checkout's files are where configs live. If the tools
are missing, run `/rightsize:setup` first.

## Rules that hold throughout
- **Spend only with consent.** Plan first (`plan_run`), show the user the plan (calls, estimate,
  anything that blocks it), and call `start(plan_id)` only after they agree.
- **Keys stay out of the conversation.** Configs name environment variables; values go in the
  checkout's `.env`. Never ask for or echo a key.
- **Third parties are untrusted.** A config for anyone else's system says `trusted: false`; the
  held-out split never goes to it, and the plan enforces that.
- **Held-out stays here.** A held-out run comes back as aggregates only; its items and error text
  stay in the Workbench. Don't try to work around that.
- **Relay numbers with their caveats.** Every score with its 95% interval and n; errors and
  refusals separately, never as wrong answers; cost `unknown` is not $0; quote the `caveats`
  sentences. See `references/reading-a-scorecard.md`.

## 1. Find out how to reach the system
Ask what it is and how it's called, or read its API docs if the user points at them.
- **Speaks rightsize/v1** (`GET /rightsize/v1/info`, `POST /rightsize/v1/run`): use its URL
  directly as `system`.
- **Its own HTTP or MCP API**: write a declarative config (step 2).
- **A model the reference system can call**: `system="ref:llm"` with `model` and `params`
  (`provider`, `base_url`, `api_key_env`).

## 2. Write the config (own API only)
Start from the closest template in the checkout's `configs/systems/examples/`:
`search-api-template.yaml` (retrieval over a search API), `document-review-api.yaml` (upload a
document, start a review, poll, fetch results), `bm25-declarative-mcp.yaml` (an MCP tool).
Save it as `configs/systems/<name>.yaml`. The format, every field, and the vocabulary maps are in
`references/config-reference.md`. Mark tasks the system can't do as `unsupported`.

## 3. Prove the mapping
`check_conformance(system="configs/systems/<name>.yaml", suite="m1")` sends two real items per
task type; if the system calls a paid model, tell the user first. A failure is a mapping
bug, not a weak model: fix the JSONPaths, status `map`, or transforms it names, and check again.
Stop after five attempts and show the user the last findings.

## 4. Plan, agree, run
1. `plan_run(system=..., suite="m1", repeats=3, max_cost=<cap>)`. A system that calls paid models
   needs a cap; one with no price on file needs `ack_unpriced=true` and the user's agreement that
   its cost will show as unknown.
2. If the estimate says no cost is measured yet, offer a pilot first: the same plan with
   `limit=20, repeats=1, max_cost=5`. Its measured cost per task feeds the next plan's estimate.
3. Show the plan. On agreement, `start(plan_id)`. If `start` answers that the plan changed, show
   the new plan instead.
4. Follow with `job_status(job_id)` about once a minute. Tell the user they can watch it in the
   Workbench (`uv run rightsize ui` in the checkout). `cancel_job` stops it.
Run suite `m2` (business associate agreements) the same way when the system reviews contracts.

## 5. Report
`summarize_run(run_id)`, then for each task type:
- the headline metric, its interval, n, and whether it clears the trivial predictors;
- errors and missing answers, with the hint for each error group;
- cost per task, or that it's unknown;
- the caveats, quoted;
- the weakest tiers (e.g. `query_style=scenario`, `presentation=vendor_paper`).
Then open two or three of `worst_items` with `get_item` and explain the failure mode from what
was asked, how the expected label was built, and what the system answered.

## 6. Compare
Against the free baseline (`ref:bm25` on the same suite) or the system's previous version:
`diff_runs(run_a, run_b)`. Lead with `changed` (more than the model may differ), then the primary
metric's paired delta, then items that flipped.
