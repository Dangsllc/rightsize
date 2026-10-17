# Rightsize

An objective, system-agnostic benchmark harness for compliance AI, and a way to find the
**cheapest model that's good enough** for each task.

- **Any system.** Reach it over HTTP or MCP: natively (it speaks the two-call
  [`rightsize/v1` protocol](spec/rightsize-protocol-v1.md)) or through a YAML config that maps our tasks onto its
  own API. No code on the system's side.
- **Labels by construction.** Every expected answer comes from how the item was built, not from
  someone's judgment: queries written for one known provision, and policy manuals with specific
  required elements deliberately removed or weakened.
- **Honest scores.** Bootstrap confidence intervals, trivial-predictor floors, errors and
  missing answers counted separately (never as wrong answers), cost reported only when the system
  reports usage, and a manifest that pins everything needed to reproduce a run.

## Quick start
```bash
uv sync --all-packages
uv run rightsize data check                          # verify the committed dataset
uv run rightsize run --system ref:bm25               # in-process reference system
uv run rightsize serve bm25 --port 8701 &            # ...or serve it over rightsize/v1
uv run rightsize run --system http://127.0.0.1:8701
uv run rightsize run --system ref:llm --model claude-haiku-4-5 --repeats 3   # needs ANTHROPIC_API_KEY
uv run rightsize diff runs/<A> runs/<B>              # paired comparison, flags regressions
```
Each run writes `runs/<id>/` with `manifest.json`, `results.jsonl`, `scorecard.json`, and a
self-contained `scorecard.html`.

## Workbench
`uv run rightsize ui` opens a local web UI over the same files the CLI writes. It listens on
127.0.0.1 only, and the URL it prints carries a session token.
- **Home** lists what needs a person: runs that measured nothing (every call failed), runs whose
  process died, sweeps that stopped, routing decisions resting on fewer than 30 clusters, and API keys
  the ladders need but `.env` lacks.
- **Runs** put health and caveats before scores. Each primary metric is drawn as its 95% interval
  against trivial predictors, and every tier links down to its items: what was asked, how the expected
  answer was built, and what the system said.
- **Compare** lists what changed between two runs before the paired deltas, then the items that flipped.
- **Sweeps** draw cost against quality per rung. Moving the threshold, margin, or floor re-decides
  routing instantly; Save reruns `calibrate`, so `routing.yaml` matches the CLI.
- **Launch** builds a plan first: calls, cost measured in earlier runs (never guessed), and guards
  for held-out data, the spend cap, and missing keys. Only then does it start the run in the
  background. A pilot measures cost on 20 tasks before the full run.

## Testing your own system
1. Copy a template from [`configs/systems/examples/`](configs/systems/examples/) (search API,
   async document-review API, MCP server) and fill in endpoints and JSONPaths.
2. `uv run rightsize conformance --system my-system.yaml`: proves responses map onto the contract.
3. `uv run rightsize run --system my-system.yaml --repeats 3`.

Third-party systems never receive the held-out split (`trusted: true` is required).

## Suite m1 (HIPAA)
| Task | What it measures | Headline metric |
|---|---|---|
| `retrieval` | Find the provision of 45 CFR 160/164 that answers a question (exact-term, paraphrase, and scenario tiers) | nDCG@10 |
| `control_classification` | Read a policy manual and rate each Security Rule requirement covered / partial / gap | group macro-F1 |

See [`data/DATASHEET.md`](data/DATASHEET.md) for how items are built and their known limits.

## Suite m2 (business associate agreements)
| Task | What it measures | Headline metric |
|---|---|---|
| `retrieval` | Find the provision a BAA implements or depends on, including from a contract clause | nDCG@10 |
| `control_classification` | Read a whole BAA and rate 15 contract requirements covered / partial / gap, in two presentations of the same content (a standalone BAA, and vendor paper with the terms in an exhibit) | group macro-F1 |

`uv run rightsize run --system ... --suite m2`. Each labelled agreement exists in both presentations, so the
`presentation` stratum shows how much a model loses to drafting style alone. See the m2 section of the datasheet.

A curated, hand-verified "golden questions" suite is planned as a later addition alongside m1 and m2.

## Layout
`packages/rightsize-core` (domain-agnostic harness) · `packages/rightsize-pack-compliance` (task pack) ·
`packages/rightsize-reference-systems` (bm25, llm, oracle) · `spec/` (protocol) · `data/` (CC BY 4.0) ·
`configs/` (suites, pricing, system examples).

Code: Apache-2.0. Data: CC BY 4.0. Regulatory text is US public domain.
