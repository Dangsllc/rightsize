"""The MCP server: agents get the same numbers as scorecard.json and the CLI, every number carries
its caveats, and nothing spends money without a plan the user saw."""

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from rightsize_core.calibration.sweep import decide
from rightsize_core.cli import app
from rightsize_core.mcp_server import build_server
from rightsize_core.systems.mcp_client import MCPConnection, MCPToolError
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]
READ_TOOLS = {"list_runs", "summarize_run", "list_items", "get_item", "diff_runs", "list_sweeps", "get_calibration",
              "preview_routing", "list_jobs", "job_status", "describe_suite", "validate_config"}
WRITE_TOOLS = {"start", "calibrate", "cancel_job"}


def call(server, tool: str, args: dict):
    async def go():
        conn = MCPConnection(server)
        try:
            return await conn.call(tool, args)
        finally:
            await conn.close()
    return asyncio.run(go())


@pytest.fixture(scope="module")
def server(states):
    return build_server(ROOT, states[0])


def test_tools_say_whether_they_change_anything(server):
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert READ_TOOLS | WRITE_TOOLS | {"check_conformance", "plan_run", "plan_sweep"} == set(tools)
    for name in READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    for name in WRITE_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name
    assert "user has seen the plan" in tools["start"].description


def test_summaries_match_the_scorecard(server, states):
    for state in ("run_ok", "run_unknown_cost", "run_aborted"):
        rid = states[1][state]
        s = call(server, "summarize_run", {"run_id": rid})
        card = json.loads((states[0] / rid / "scorecard.json").read_text())
        for h in s["headlines"]:
            m = card["task_types"][h["task_type"]]["metrics"][h["metric"]]
            assert h["value"] == round(m["value"], 4) and h["ci95"] == [round(m["lo"], 4), round(m["hi"], 4)]
            assert h["n_clusters"] == m["n_clusters"]


def test_caveats_travel_with_the_numbers(server, states):
    unknown = call(server, "summarize_run", {"run_id": states[1]["run_unknown_cost"]})
    assert unknown["headlines"][0]["cost_state"] == "unknown" and unknown["headlines"][0]["cost_per_task_usd"] is None
    assert any("not $0" in c for c in unknown["caveats"])
    capped = call(server, "summarize_run", {"run_id": states[1]["run_aborted"]})
    assert capped["headlines"][0]["tiny_n"] and any("too few" in c for c in capped["caveats"])
    dead = call(server, "summarize_run", {"run_id": states[1]["run_no_signal"]})
    assert dead["health"] == "no_signal" and dead["error_groups"][0]["hint"]
    assert all(h["value"] is None for h in dead["headlines"])


def test_cli_json_is_the_same_payload(server, states, tmp_path, monkeypatch):
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    rid = states[1]["run_ok"]
    runs = tmp_path / "runs"
    shutil.copytree(states[0] / rid, runs / rid)
    res = CliRunner().invoke(app, ["report", str(runs / rid), "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["headlines"] == call(server, "summarize_run", {"run_id": rid})["headlines"]


def test_items_and_documents_stay_small_unless_asked(server, states):
    rid = states[1]["run_classification"]
    items = call(server, "list_items", {"run_id": rid, "task_type": "control_classification", "show": "all"})
    task = items["items"][0]["task_id"]
    short = call(server, "get_item", {"run_id": rid, "task_id": task})
    doc = next(b for b in short["blocks"] if b["kind"] == "document")
    assert len(doc["text"]) == 1500 and "full_document" in doc["truncated"]
    full = call(server, "get_item", {"run_id": rid, "task_id": task, "full_document": True})
    assert len(next(b for b in full["blocks"] if b["kind"] == "document")["text"]) > 1500
    table = next(b for b in short["blocks"] if b["kind"] == "table" and "how it was built" in b["columns"])
    assert table["rows"]  # the construction of every label comes with the item


def test_routing_previews_match_the_rule(server, states):
    sweep = states[1]["sweep_complete"]
    cal = call(server, "get_calibration", {"sweep_id": sweep})
    assert cal["routes"][0]["chosen"] == "large" and cal["routing_yaml"]
    preview = call(server, "preview_routing", {"sweep_id": sweep, "thresholds": {"ndcg@10": 0.75}})
    saved = json.loads((states[0] / "sweeps" / sweep / "calibration.json").read_text())
    for d, p in zip(saved["decisions"], preview["decisions"], strict=True):
        assert p["chosen"] == decide(d["rungs"], d["best"], 0.75, saved["delta"], saved["floors"])[0]
    low = call(server, "get_calibration", {"sweep_id": states[1]["sweep_low_n"]})
    assert any("below 30" in c or "fewer than 30" in c for c in low["caveats"])


def test_diff_says_what_changed_first(server, states):
    d = call(server, "diff_runs", {"run_a": states[1]["run_ok_small"], "run_b": states[1]["run_ok"]})
    assert "model: 'fake-small' -> 'fake-large'" in d["changed"]
    r = d["task_types"]["retrieval"]
    assert r["primary"]["flag"] == "improved" and r["improved"] > 0 and r["regressed"] == 0


def test_unknown_ids_get_a_sentence_not_a_traceback(server):
    with pytest.raises(MCPToolError, match="list_runs, list_sweeps"):
        call(server, "summarize_run", {"run_id": "nope"})
    with pytest.raises(MCPToolError, match="plan_run or plan_sweep first"):
        call(server, "start", {"plan_id": "0000"})


def test_plan_then_start_then_follow(plugin_path, tmp_path):
    srv = build_server(ROOT, tmp_path / "runs")

    async def go():
        conn = MCPConnection(srv)
        try:
            blocked = await conn.call("plan_run", {"system": "plugin:fake_ladder_system:make", "model": "fake-small",
                                                   "task_types": ["retrieval"], "limit": 4})
            assert not blocked["ok"] and any("unknown cost" in b for b in blocked["blocking"])
            refused = await conn.call("start", {"plan_id": blocked["plan_id"]})
            assert refused["started"] is False
            plan = await conn.call("plan_run", {"system": "plugin:fake_ladder_system:make", "model": "fake-small",
                                                "task_types": ["retrieval"], "limit": 4, "ack_unpriced": True})
            assert plan["ok"] and plan["calls"] == 4 and plan["command"].startswith("uv run rightsize run")
            started = await conn.call("start", {"plan_id": plan["plan_id"]})
            assert started["started"] and "/jobs/" in started["ui_url"]
            for _ in range(150):
                status = await conn.call("job_status", {"job_id": started["job_id"]})
                if status["state"] != "running":
                    break
                await asyncio.sleep(0.2)
            assert status["state"] == "finished" and status["launched_by"] == "mcp"
            assert status["runs"][0]["health"] == "ok" and status["runs"][0]["progress"]["done"] == 4
        finally:
            await conn.close()
    asyncio.run(go())


def test_a_changed_plan_is_shown_again_not_started(tmp_path, monkeypatch):
    monkeypatch.setenv("LM_API_TOKEN", "present")
    srv = build_server(ROOT, tmp_path / "runs")
    args = {"system": "ref:llm", "model": "qwen2.5-7b-instruct", "limit": 2, "task_types": ["retrieval"],
            "params": {"provider": "openai", "base_url": "http://127.0.0.1:9", "api_key_env": "LM_API_TOKEN"}}

    async def go():
        conn = MCPConnection(srv)
        try:
            plan = await conn.call("plan_run", args)
            assert plan["ok"], plan["blocking"]
            monkeypatch.delenv("LM_API_TOKEN")
            again = await conn.call("start", {"plan_id": plan["plan_id"]})
            assert again["started"] is False and "changed" in again["reason"]
            assert any("LM_API_TOKEN" in b for b in again["plan"]["blocking"])
        finally:
            await conn.close()
    asyncio.run(go())
    assert not (tmp_path / "runs" / "jobs").exists()


def test_stdio_server_starts_from_the_cli(states):
    cmd = f"{sys.executable} -m rightsize_core.cli mcp --runs-dir {states[0]}"

    async def go():
        conn = MCPConnection(cmd, env={"RIGHTSIZE_ROOT": str(ROOT)})
        try:
            tools = await conn.list_tools()
            runs = await conn.call("list_runs", {"limit": 3})
            return tools, runs
        finally:
            await conn.close()
    t0 = time.time()
    tools, runs = asyncio.run(go())
    assert "summarize_run" in tools and runs["total"] >= 8 and len(runs["runs"]) == 3
    assert time.time() - t0 < 60


def test_cli_json_for_diff_calibrate_and_dry_run(states, tmp_path, monkeypatch):
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    monkeypatch.setenv("RIGHTSIZE_PLUGIN_PATH", str(ROOT / "tests/fixtures"))
    runner = CliRunner()
    a, b = states[0] / states[1]["run_ok_small"], states[0] / states[1]["run_ok"]
    d = runner.invoke(app, ["diff", str(a), str(b), "--json"])
    assert d.exit_code == 0 and json.loads(d.output)["task_types"]["retrieval"]["primary"]["flag"] == "improved"
    sweeps = tmp_path / "runs" / "sweeps"
    shutil.copytree(states[0] / "sweeps" / states[1]["sweep_complete"], sweeps / "s")
    c = runner.invoke(app, ["calibrate", str(sweeps / "s"), "--threshold", "ndcg@10=0.75", "--json"])
    assert c.exit_code == 0, c.output
    assert json.loads(c.output)["routes"][0]["chosen"] == "medium"
    plan = runner.invoke(app, ["sweep", "--system", "ref:bm25", "--rungs", "haiku-4.5", "--task-type", "retrieval",
                               "--dry-run", "--json"])
    assert plan.exit_code == 0, plan.output
    p = json.loads(plan.output)
    assert p["ok"] is False and any("can't be swept" in x for x in p["blocking"])


def test_heldout_runs_come_back_as_aggregates_only(states, tmp_path):
    runs = tmp_path / "runs"
    for key in ("run_ok", "run_ok_small", "run_no_signal"):
        rid = states[1][key]
        shutil.copytree(states[0] / rid, runs / rid)
        if key != "run_ok_small":
            m = json.loads((runs / rid / "manifest.json").read_text())
            m["dataset"]["split"] = "heldout"
            (runs / rid / "manifest.json").write_text(json.dumps(m))
    srv = build_server(ROOT, runs)
    rid = states[1]["run_ok"]
    s = call(srv, "summarize_run", {"run_id": rid})
    assert s["headlines"] and s["worst_items"] == {} and "never leave this machine" in s["heldout"]
    assert "m1.ret." not in json.dumps(s)  # no task ids anywhere
    dead = call(srv, "summarize_run", {"run_id": states[1]["run_no_signal"]})
    assert dead["error_groups"][0]["hint"] and "withheld" in dead["error_groups"][0]["message"]
    for tool, args in (("get_item", {"task_id": "m1.ret.164.308(a)(1)(ii)(A).exact_term"}),
                       ("list_items", {"task_type": "retrieval"})):
        with pytest.raises(MCPToolError, match="never leave this machine"):
            call(srv, tool, {"run_id": rid, **args})


def test_comparisons_with_a_heldout_run_keep_counts_not_items(states, monkeypatch):
    from rightsize_core.service import agent, queries
    from rightsize_core.service.index import Index

    real = queries.compare

    def heldout_b(ix, a, b, delta=0.0):  # the same comparison, with B on the held-out split
        c = real(ix, a, b, delta)
        return {**c, "b": c["b"].model_copy(update={"split": "heldout"})}

    monkeypatch.setattr(agent.queries, "compare", heldout_b)
    d = agent.diff_runs(Index(ROOT, states[0]), states[1]["run_ok_small"], states[1]["run_ok"])
    r = d["task_types"]["retrieval"]
    assert r["improved"] > 0 and r["improved_items"] == [] and r["regressed_items"] == []
    assert any("withheld" in c for c in d["caveats"]) and "m1.ret." not in json.dumps(d["task_types"])


def test_conformance_is_not_labelled_read_only(server):
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["check_conformance"].annotations.read_only_hint is False
    assert "confirm first" in tools["check_conformance"].description
