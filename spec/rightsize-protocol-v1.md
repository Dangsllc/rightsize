# rightsize/v1: the Rightsize system protocol

A system under test implements two operations. That's the whole contract.

| Operation | HTTP | MCP tool |
|---|---|---|
| Describe yourself | `GET /rightsize/v1/info` → `SystemInfo` | `rightsize_info()` → `SystemInfo` |
| Do one task | `POST /rightsize/v1/run` with `RunRequest` → `Outcome` | `rightsize_run(task_type, inputs, model?, params?)` → `Outcome` |

Schemas: [`schemas/`](schemas/) (generated from code by `rightsize spec`; CI fails on drift).

## SystemInfo
`name`, `version`, `task_types` (which tasks you accept), `granularity` per task type
(`spec`, `group`, or `document`), `supports_model_param` (you honor `model` in `RunRequest`;
required for `rightsize sweep`), `models`, `efforts`, `reports_usage`, `deterministic`.

## RunRequest
`task_type`, `inputs` (shape defined by the task pack, see the `*.input.schema.json` files),
`model` (optional), `params` (optional, free-form; e.g. `effort`, `temperature`, `mode`).

## Outcome
- `prediction`: shape defined by the task pack (`*.prediction.schema.json`).
- `usage`: list of `{provider, model, input_tokens, output_tokens, thinking_tokens?, ...}`.
  Omit it if you can't report it; cost is then shown as **unknown**, never estimated.
- `params_sent`: what you actually sent to the model (not what was requested).
- `error`: `{kind: error | refusal | timeout | unsupported | mapping, message}`. An errored task
  is never scored as a wrong answer: errors, refusals, and missing answers are counted
  separately.
- `latency_ms`, `latency_kind` (`model` or `end_to_end`), `path` (which code path answered).

Long-running work over HTTP may answer `202 Accepted` with a `Location` header; the harness polls
that URL until it gets a non-202 response containing the `Outcome`.

## Not speaking rightsize/v1?
Write a declarative config instead (no code on your side): see
[`configs/systems/examples/`](../configs/systems/examples/). It maps our tasks onto your own HTTP
or MCP API (steps, polling, JSONPath extraction, vocabulary maps). Run `rightsize conformance` first;
it fails loudly when the mapping, not the model, is the problem.

## Compliance task pack (`compliance`)
- `retrieval`: `{query, k}` → `{hits: [{citation, score?, text?, citation_source}]}`. Citations are
  normalized (`45 CFR § 164.312(a)(2)(iv)` ≡ `164.312(a)(2)(iv)`).
- `control_classification`: `{document_id, document, document_sha256, units[], groups{}}` →
  `{assessments: [{control_code, status, confidence?, evidence?, abstained?}]}` with `status` in
  `covered | partial | gap | not_applicable`. Answer per unit (spec) or per requirement group;
  both are scored.
