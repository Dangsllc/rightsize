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

A curated, hand-verified "golden questions" suite is planned as a later addition alongside m1.

## Layout
`packages/rightsize-core` (domain-agnostic harness) · `packages/rightsize-pack-compliance` (task pack) ·
`packages/rightsize-reference-systems` (bm25, llm, oracle) · `spec/` (protocol) · `data/` (CC BY 4.0) ·
`configs/` (suites, pricing, system examples).

Code: Apache-2.0. Data: CC BY 4.0. Regulatory text is US public domain.
