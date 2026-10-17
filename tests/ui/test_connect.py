"""Connect a system in the Workbench: problems with lines, a traced item, JSONPath, conformance,
and saving only where it should."""

import html
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rightsize_ui.app import create_app

ROOT = Path(__file__).resolve().parents[2]
HOST = "http://127.0.0.1:8765"

DRAFT = f"""name: bm25-draft
transport: mcp
server: {sys.executable} -m rightsize_core.cli serve bm25 --transport mcp-stdio
trusted: false
tasks:
  retrieval:
    steps:
      - tool: rightsize_run
        args: {{task_type: retrieval, inputs: {{query: "{{{{ inputs.query }}}}", k: "{{{{ inputs.k }}}}"}}}}
    extract:
      hits:
        each: "$.prediction.hits[*]"
        fields:
          citation: {{path: $.citation, transform: citation}}
          score: {{path: $.score, type: float}}
  control_classification: unsupported
"""


@pytest.fixture()
def web(states, tmp_path, monkeypatch):
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    c = TestClient(create_app(ROOT, states[0], token="t", systems_dir=tmp_path / "local"), base_url=HOST)
    c.cookies.set("rightsize_session", "t")
    return c


def act(web, action: str, text: str = DRAFT, **extra):
    data = {"text": text, "name": "bm25-draft", "suite": "m1", "task_type": "retrieval", "action": action, **extra}
    return web.post("/systems/connect", data=data, headers={"origin": HOST})


def test_templates_and_drafts_open(web):
    assert web.get("/systems/new").status_code == 200
    page = web.get("/systems/new?template=document-review-api").text
    assert "POST /documents" in page and "REVIEW_API_TOKEN" in page
    assert "<option selected>control_classification</option>" in page  # the task it supports
    assert "Connect a new system" in web.get("/systems").text


def test_only_listed_configs_can_be_opened(web):
    assert web.get("/systems/edit", params={"spec": "../../etc/passwd"}).status_code == 404
    assert web.get("/systems/edit", params={"spec": "pyproject.toml"}).status_code == 404


def test_check_shows_problems_on_their_lines(web):
    page = act(web, "check", DRAFT.replace("      - tool:", "      - tol:")).text
    assert 'data-goto-line="8"' in page and "unknown key `tol`" in page
    assert "Well formed" in act(web, "check").text


def test_try_one_item_traces_each_step_and_tests_jsonpath(web):
    page = html.unescape(act(web, "try").text)
    assert "One item, step by step" in page and "Step 1" in page and "rightsize_run" in page
    assert '"citation": "164.' in page  # the extracted prediction
    trace = re.search(r'name="trace_id" value="([0-9a-f]+)"', page).group(1)
    jp = act(web, "jsonpath", trace_id=trace, step="0", expr="$.prediction.hits[*].citation").text
    assert re.search(r"\b10 matches\b", jp)
    bad = act(web, "jsonpath", trace_id=trace, step="0", expr="$$[").text
    assert "Not a JSONPath" in bad


def test_conformance_passes_and_failures_point_at_lines(web):
    assert "Answers map cleanly" in act(web, "conformance").text
    broken = DRAFT.replace('each: "$.prediction.hits[*]"', 'each: "$.prediction.results[*]"')
    page = act(web, "conformance", broken).text
    assert "These would be scored as a weak system" in page and "data-goto-line=" in page


def test_save_goes_to_the_local_folder_and_never_clobbers(web, tmp_path):
    page = act(web, "save").text
    saved = tmp_path / "local" / "bm25-draft.yaml"
    assert saved.read_text() == DRAFT and "Saved to" in page
    assert "already exists" in act(web, "save").text
    assert "Saved to" in act(web, "save", overwrite="on").text
    assert "lowercase letters" in act(web, "save", name="../evil").text
    assert not (tmp_path / "evil.yaml").exists()
    assert "bm25-draft" in web.get("/systems").text  # listed, with an Edit link


def test_hand_to_claude_carries_the_draft_and_its_problems(web):
    page = act(web, "check", DRAFT.replace("type: float", "type: number")).text
    assert "benchmark-my-system" in page and "line 15" in page


def test_suites_page_counts_without_listing_heldout(web):
    page = web.get("/suites").text
    assert "m1" in page and "m2" in page and "Data matches its manifest" in page
    assert "query_style" in page and "presentation" in page
