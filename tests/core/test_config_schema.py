"""Config validation points at the exact line, and conformance findings find their way back to it."""

from pathlib import Path

import pytest
from rightsize_core.systems.config_schema import line_of, locate, validate_config

ROOT = Path(__file__).resolve().parents[2]

BAD = """name: x
transport: http
base_url: http://127.0.0.1:9
tasks:
  retrieval:
    steps:
      - call: POST /search
        sav: {id: $.id}
    extract:
      hits:
        each: $.results[*]
        fields:
          cite: $.c
          score: {path: $.s, type: number}
  control_classification:
    steps:
      - call: GET /review
      - poll: {call: "GET /review/{{ id }}", until: {path: $.state, in: [done]}}
    extract:
      assessments:
        each: $.r[*]
        fields:
          control_code: {path: $.req, transform: citatoin}
          status: {path: $.rating, map: {met: covered, nope: failed}}
"""


@pytest.mark.parametrize("path", sorted((ROOT / "configs/systems/examples").glob("*.yaml")), ids=lambda p: p.name)
def test_shipped_examples_have_no_errors(path):
    assert [p for p in validate_config(path.read_text()) if p.severity == "error"] == []


def test_each_mistake_is_reported_once_on_its_line():
    got = {(p.line, p.message.split(";")[0]) for p in validate_config(BAD) if p.severity == "error"}
    assert got == {
        (8, "unknown key `sav` (a typo?)"),
        (13, "`cite` isn't part of the contract"),
        (14, "Input should be 'float' or 'bool'"),
        (23, "no transform called 'citatoin'"),
        (24, "maps to failed, which isn't allowed"),
    }


def test_structure_rules():
    def errors(text):
        return [p.message for p in validate_config(text) if p.severity == "error"]

    assert any("needs `base_url`" in m for m in errors("name: x\ntransport: http\ntasks: {retrieval: unsupported}\n"))
    assert any("exactly one of `call`" in m for m in errors(
        "name: x\ntransport: http\nbase_url: h\ntasks:\n  retrieval:\n    steps: [{body: {}}]\n    extract: {}\n"))
    assert any("METHOD /path" in m for m in errors(
        "name: x\ntransport: http\nbase_url: h\ntasks:\n  retrieval:\n    steps: [{call: search}]\n    extract: {}\n"))
    assert any("no task type 'summaries'" in m for m in errors(
        "name: x\ntransport: http\nbase_url: h\ntasks:\n  summaries: {steps: [], extract: {}}\n"))
    assert errors("name: x\nprotocol: rightsize/v1\ntransport: http\nbase_url: http://h\n") == []


def test_unquoted_brackets_in_flow_style_get_a_fix():
    [p] = validate_config("name: x\ntransport: http\nsave: {ids: $.items[*].id}\n")
    assert p.line == 3 and "needs quotes" in p.message


def test_unset_env_vars_and_trust_are_warnings(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    text = ("name: x\ntransport: http\nbase_url: ${NOT_SET_ANYWHERE}\ntrusted: true\n"
            "tasks: {retrieval: unsupported}\n")
    warnings = {(p.line, p.severity) for p in validate_config(text)}
    assert (3, "warning") in warnings and (4, "warning") in warnings


def test_findings_point_at_the_config_line():
    assert locate(BAD, "control_classification", "values with no mapping: status='non_compliant'") == 24
    assert locate(BAD, "retrieval", "prediction does not match contract: [{'loc': ('hits', 0, 'cite')}]") == 13
    assert locate(BAD, "control_classification", "error: StepError: GET /review/abc -> HTTP 404: nope") == 17
    assert locate(BAD, "retrieval", "mapped 40% of expected units — below 95%; check the extract paths") == 9
    assert locate("base_url: ${API_URL}\n", None, "Environment variable API_URL is not set") == 1
    assert line_of(BAD, ["tasks", "retrieval", "nonexistent"]) == 5  # nearest ancestor
