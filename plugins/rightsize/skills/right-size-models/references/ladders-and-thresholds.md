# Ladders, prices, thresholds, and routing.yaml

## A ladder
`configs/ladders/<name>.yaml`, cheapest rung first:
```yaml
rungs:
  - name: qwen2.5-7b@lmstudio          # what results and routing call it
    model: qwen2.5-7b-instruct         # sent to the system as `model`
    concurrency: 1                     # optional; local servers want 1
    params: {provider: openai, base_url: "http://127.0.0.1:1234/v1", api_key_env: LM_API_TOKEN,
             temperature: 0, max_tokens: 8000, mode: spec}
  - name: sonnet-5.5@low
    model: claude-sonnet-5-5
    params: {effort: low}
```
`params` go to the system with every call. For `ref:llm`: `provider` (`anthropic`, `openai` for
any OpenAI-compatible server, or `ollama`), `base_url`, `api_key_env` (the variable holding the
key), and model settings such as `effort`, `reasoning_effort`, `temperature`, `max_tokens`.
`no_heldout: true` marks a rung whose provider may train on prompts (e.g. a free tier); such a
rung refuses the held-out split.

## Prices
`configs/pricing/<date>.yaml` maps each model to USD per million tokens:
`claude-sonnet-5-5: {input: 2.00, output: 10.00, cache_read: 0.20}`. Thinking tokens bill as
output. A local model is `{input: 0.0, output: 0.0}`. A rung without an entry has unknown cost,
the spend cap can't stop it, and calibration orders rungs by the ladder instead of by cost.

## Thresholds
`thresholds` maps a metric to its bar; the metric must be one the task type reports. Use
`task_type:metric` to set it for one task type only.
- Retrieval: `ndcg@10`, `recall@5`, `mrr`.
- Classification: `group.macro_f1`, `spec.macro_f1`, `group.kappa`, `spec.deficiency_recall`.
A rung is good enough when, with under 2% errors and every floor cleared, either its lower 95%
bound clears the threshold, or a paired comparison puts its lower bound within `delta` of the
best rung. The cheapest such rung wins. With no threshold, only the margin test applies.

`floors` map a metric to a minimum every chosen rung must meet, e.g.
`{"spec.deficiency_recall": 0.9}`.

## Evidence limits
A decision on fewer than 30 clusters is `insufficient_n`: it's shown, but routing.yaml makes no
tier override from it, and an overall route that thin isn't a finding. m2 retrieval is small
enough that its routes usually carry this flag; on the public split, m2 classification and m1
retrieval clear it. A sampled or dev-split sweep rarely does.

## routing.yaml
```yaml
routes:
  retrieval:
    model: claude-haiku-5-5
    params: {temperature: 0}
    evidence: {metric: ndcg@10, lower_bound: 0.712, cost_per_task_usd: 0.0004,
               why: "lower bound 0.712 >= 0.6", n_clusters: 50, insufficient_n: false}
stratum_overrides:
  retrieval:
    query_style=scenario: {model: claude-sonnet-5-5, params: {effort: low}}
```
A consumer routes each task type to `routes[task_type]`, and to an override when the item's tier
matches one.
