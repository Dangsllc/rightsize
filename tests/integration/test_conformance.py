import asyncio
import copy
from pathlib import Path

import yaml
from rightsize_core.conformance.check import run_conformance
from rightsize_core.systems.declarative import DeclarativeSystem, Extractor, render
from rightsize_pack_compliance.pack import PACK
from rightsize_reference_systems.registry import make
from rightsize_reference_systems.server import mcp_server

ROOT = Path(__file__).resolve().parents[2]
BASE = yaml.safe_load((ROOT / "configs/systems/examples/bm25-declarative-mcp.yaml").read_text())


def _conform(cfg):
    sysobj = DeclarativeSystem(cfg, PACK.transforms, mcp_server_override=mcp_server(make("bm25", ROOT)))

    async def go():
        try:
            await sysobj.preflight()
            return await run_conformance(ROOT, sysobj, "m1")
        finally:
            await sysobj.aclose()

    return asyncio.run(go())


def test_good_mapping_passes():
    rep = _conform(copy.deepcopy(BASE))
    assert rep.ok, rep.text()


def test_wrong_citation_path_fails():
    cfg = copy.deepcopy(BASE)
    cfg["tasks"]["retrieval"]["extract"]["hits"]["fields"]["citation"] = {"path": "$.cite", "transform": "citation"}
    rep = _conform(cfg)
    assert not rep.ok
    assert "mapped 0%" in rep.text()


def test_wrong_list_path_fails():
    cfg = copy.deepcopy(BASE)
    cfg["tasks"]["retrieval"]["extract"]["hits"]["each"] = "$.results[*]"
    rep = _conform(cfg)
    assert not rep.ok


def test_extractor_reports_unmapped_status_values():
    ex = Extractor()
    spec = {"assessments": {"each": "$.findings[*]", "fields": {
        "control_code": "$.code",
        "status": {"path": "$.status", "map": {"compliant": "covered", "non_compliant": "gap"}},
    }}}
    data = {"findings": [{"code": "164.312(b)", "status": "Compliant"},
                         {"code": "164.312(d)", "status": "needs_attention"}]}
    pred = ex.build(spec, data)
    assert pred["assessments"][0]["status"] == "covered"  # case-insensitive match
    assert "status" not in pred["assessments"][1]
    assert ex.unmapped == [("status", "needs_attention")]


def test_render_keeps_types_for_whole_expressions():
    ctx = {"inputs": {"k": 10, "units": [{"a": 1}]}, "model": None}
    assert render("{{ inputs.k }}", ctx) == 10
    assert render("{{ inputs.units }}", ctx) == [{"a": 1}]
    assert render("k={{ inputs.k }}", ctx) == "k=10"
