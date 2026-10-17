# Declarative system configs

A config maps Rightsize's tasks onto a system's own HTTP or MCP API, with no code on the system's
side. It lives at `configs/systems/<name>.yaml` in the checkout; the examples in
`configs/systems/examples/` are working starting points.

## Top level
| Key | Meaning |
|---|---|
| `name` | The system's name in results. |
| `transport` | `http` or `mcp`. |
| `base_url` | HTTP root, e.g. `${MY_API_URL}`. `${VAR}` reads an environment variable. |
| `server` | MCP only: a command (stdio) or a URL (streamable HTTP). `env:` passes variables to it. |
| `auth` | `{type: bearer, env: MY_TOKEN}`, or `{name: X-API-Key, env: MY_KEY}` for a header. Names a variable; the value lives in `.env`. |
| `headers` | Extra headers; may use `${VAR}`. |
| `trusted` | `false` for anyone else's system. Only `true` systems ever get the held-out split. |
| `throttle` | `{rps: 2, concurrency: 2}`. |
| `timeout_s` | Per HTTP request (default 600). |
| `model_param` | Present when the system accepts a model choice; then `{{ model }}` is available and the system can be swept. |
| `setup`, `teardown` | Steps run once before the first task and after the last (e.g. look up an id). Values they `save` are available to every task. |
| `healthcheck` | One step run by preflight; a failure stops the run before it starts. |
| `usage` | How to read token usage, so cost can be computed: `{from: last, each: $.usage[*], fields: {model: $.model, input_tokens: $.input_tokens, output_tokens: $.output_tokens}}`. Without it, cost is reported as unknown. |

## Tasks
```yaml
tasks:
  retrieval: unsupported          # the system can't do this task
  control_classification:
    granularity: group            # answers per requirement group (spec answers per requirement)
    steps: [...]
    extract: {...}
    extract_from: last            # which saved response to extract from (default: the last step's)
    cleanup: [...]                # steps run after each task, even on failure
```

## Steps
Each step is one of:
- `call: POST /v1/search` with optional `body` (JSON), `query`, `multipart` + `form`, and
  `allow_error: true` to keep going on an HTTP error.
- `tool: tool_name` with `args` (MCP).
- `poll:` a `call` or `tool` repeated `every_s` seconds until `until: {path: $.state, in: [done]}`,
  failing on `fail_on: [failed]` or after `timeout_s`.
- `set: {name: "{{ expr }}"}` to compute a value.

`save: {review_id: $.id}` keeps a value from the step's response for later steps.

Strings are Jinja templates. Available: `inputs` (the task's inputs), `model`, `params`,
`binding`, and every saved value. A string that is exactly one `{{ expr }}` keeps its type, so
`k: "{{ inputs.k }}"` sends a number. An unknown name is an error, not an empty string.

Task inputs:
- `retrieval`: `inputs.query`, `inputs.k`.
- `control_classification`: `inputs.document_id`, `inputs.document` (the full text),
  `inputs.document_sha256`, `inputs.units` (each with `control_code`, `title`, `specification`,
  `group`), `inputs.groups` (group -> member codes).

## Extract
Builds the prediction the pack scores.

Retrieval: `{hits: [{citation, score?, text?}]}`.
```yaml
extract:
  hits:
    each: $.results[*]
    fields:
      citation: {path: $.metadata.citation, transform: citation}   # normalizes "45 CFR § 164.312(a)"
      score: {path: $.score, type: float}
      text: $.text
```
If the API returns only prose, `hits: {from_text: $.answer, transform: citations_in_text}` pulls
citations out of it; these are scored separately as inferred.

Classification: `{assessments: [{control_code, status, confidence?, evidence?, abstained?}]}`
with `status` one of `covered`, `partial`, `gap`, `not_applicable`.
```yaml
extract:
  assessments:
    each: $.results[*]
    fields:
      control_code: {path: $.requirement, transform: citation}
      status: {path: $.rating, map: {met: covered, partially_met: partial, not_met: gap, "n/a": not_applicable}}
      confidence: {path: $.score, type: float}
      evidence: $.excerpt
      abstained: {path: $.flagged_for_human, type: bool, default: false}
```
A field is a JSONPath string, or a dict with `path`, `map` (case-insensitive vocabulary
translation; unmapped values are dropped and show up in conformance), `transform`, `type`
(`float` or `bool`), `default`, or `const`. `limit` caps a list.

## Checking it
`check_conformance(system="configs/systems/<name>.yaml")` sends two public items per task type and
fails loudly when answers don't map: missing citations, unmapped statuses, empty lists. A config
that fails conformance would be scored as a weak system, so fix it first.
