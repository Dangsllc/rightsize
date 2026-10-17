# Reading what the tools return

## Health: did the run measure anything?
| health | Meaning | What to say |
|---|---|---|
| `ok` | Finished; under 2% of answers errored or went missing. | Report the scores. |
| `degraded` | Finished, but more than 2% errored or went missing. | Report scores and the error share; errors are not counted as wrong. |
| `no_signal` | Finished with nothing to score (e.g. every call failed). | Report the error group and its hint, not a score. |
| `aborted` | Stopped by a guard: spend cap, the first calls all failing, cancel, or a crash. | Say which, from `health_reason`. Scores cover what finished. |
| `interrupted` | The process died mid-run. | Partial; `rightsize report <path>` scores what finished. |
| `running` | Still going. | Follow with `job_status`. |
| `unscored` | Finished but not scored yet. | `summarize_run` scores it. |

## A headline
`{metric, value, ci95: [lo, hi], n_clusters, low_n, tiny_n, floors, clears_floors, cost_per_task_usd, cost_state}`
- **The interval is the claim.** "ndcg@10 0.43 (95% interval 0.36 to 0.50, 50 clusters)". A
  cluster is a target provision or a labelled document; repeats of one item don't add clusters.
- `low_n`: fewer than 30 clusters. Calibrate makes no routing claim from it; say so.
- `tiny_n`: fewer than 10. The interval isn't meaningful; say "too few items to estimate".
- `floors` are trivial predictors (e.g. always answering the most common label).
  `clears_floors` is whether the lower bound beats all of them. A system that doesn't clear the
  floors isn't doing better than guessing.
- `cost_state`: `known` (measured from reported usage), `zero` (a model priced at $0, such as a
  local one), or `unknown` (no usage reported). Unknown is not $0.

## Metrics
- Retrieval: `ndcg@10` (primary), `recall@5`, `mrr`, `confusion@k` (a confusable provision ranked
  above the target; lower is better), `recall@5_lenient` (partial credit for parents and children).
- Classification: `group.macro_f1` (primary: per requirement group), `spec.macro_f1` (per
  requirement), `*.deficiency_recall` (share of gaps and partials caught; matters most to a
  compliance reviewer), `*.kappa`, `*.ece` and `*.brier` (calibration of stated confidence).

## Tiers
Scores per stratum show where a system breaks:
- `query_style`: `exact_term` (wording from the regulation), `paraphrase`, `scenario` (a
  situation, no regulatory words), `clause` (contract language).
- `writer`: who wrote the item; a big gap between writers suggests the system fits one style.
- `presentation` (m2): the same agreement as a standalone BAA or as vendor paper with the terms
  in an exhibit; a drop is pure presentation sensitivity.
- `mutation` (classification): how a requirement was altered: `intact`, `remove_section`,
  `hollow`, `drop_element`, `weaken`. Weakened wording ("may" for "shall") is the hardest.

## Errors
`error_groups` are failed calls grouped by kind and message, each with a `hint` (wrong key,
nothing listening, model not loaded, rate limit, context too long, mapping). Errors and refusals
are counted separately and never scored as wrong answers.

## Comparing runs
`diff_runs` gives B minus A paired on shared items. A metric is flagged `regression` or
`improved` only when its whole 95% interval is past zero. Read `changed` first: a different
prompt, commit, or dataset can explain a difference better than the model can.
