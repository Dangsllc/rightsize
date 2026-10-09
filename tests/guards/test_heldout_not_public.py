"""Nothing committed under data/ may let someone rebuild the held-out split."""

import json
from pathlib import Path

import yaml
from rightsize_pack_compliance.gen.corpus import Corpus
from rightsize_pack_compliance.gen.retrieval import heldout_targets

ROOT = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((ROOT / "configs/suites/m1.yaml").read_text())


def test_public_seed_rows_exclude_heldout_targets():
    corpus = Corpus.load([ROOT / s for s in CFG["sources"]])
    held = heldout_targets(corpus, CFG)
    rows = [json.loads(x) for x in (ROOT / "data/seeds/retrieval_queries.v1.jsonl").read_text().splitlines()]
    assert held and not [r["control_code"] for r in rows if r["control_code"] in held]
    assert all(r.get("canary") == CFG["canary"] for r in rows)


def test_heldout_bases_are_not_published():
    ccfg = CFG["control_classification"]
    for base in ccfg["bases"]:
        public_copy = ROOT / ccfg["seeds_dir"] / f"{base['id']}.v1.json"
        if base.get("heldout"):
            assert not public_copy.exists(), f"held-out base {base['id']} is under data/"
        elif public_copy.exists():
            assert json.loads(public_copy.read_text()).get("canary") == CFG["canary"]


def test_public_files_hold_no_heldout_tasks():
    sdir = ROOT / "data/suites/m1"
    manifest = json.loads((sdir / "MANIFEST.json").read_text())
    assert all("heldout" not in counts for counts in manifest["counts"].values())
    assert "heldout_ids_sha256" not in manifest
    for p in sdir.glob("*.jsonl"):
        assert all(json.loads(x)["split"] != "heldout" for x in p.read_text().splitlines())
    report = (sdir / "BUILD_REPORT.json").read_text()
    assert '"heldout"' not in report
