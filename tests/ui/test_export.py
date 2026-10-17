"""Exports are static, self-contained, and refuse anything that must not be published."""

import json
import re
import shutil
from pathlib import Path

import pytest
from rightsize_core.report.export import ExportRefused, export_bundle
from rightsize_core.service.index import Index

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def bundle(states, tmp_path):
    ids = states[1]
    ix = Index(ROOT, states[0])
    out = tmp_path / "results"
    record = export_bundle(ROOT, ix, out, sweeps=[ids["sweep_complete"]], runs=[ids["run_unknown_cost"]],
                           compares=[(ids["run_ok_small"], ids["run_ok"])], title="Fixture results")
    return out, record, ids


def test_bundle_is_static_and_complete(bundle):
    out, record, _ = bundle
    pages = list(out.rglob("*.html"))
    assert (out / "index.html").exists() and (out / "export.json").exists()
    assert len(record["runs"]) == 4 and len(record["sweeps"]) == 1 and len(record["compares"]) == 1
    for page in pages:
        html = page.read_text()
        assert "<script" not in html and "<link" not in html, page
        assert not re.search(r'(?:src|href)="(?:https?:)?//', html), page
        for href in re.findall(r'href="([^"#]+)"', html):
            assert (page.parent / href).resolve().exists(), (page, href)


def test_every_file_is_hashed_and_local_paths_are_gone(bundle, states):
    import hashlib

    out, record, _ = bundle
    for path, sha in record["files"].items():
        text = (out / path).read_text()
        assert hashlib.sha256(text.encode()).hexdigest() == sha
        assert str(ROOT) not in text and str(states[0].parent) not in text and str(Path.home()) not in text, path


def test_sweep_page_has_the_frontier_sensitivity_and_routing(bundle):
    out, _, ids = bundle
    html = (out / "sweeps" / ids["sweep_complete"] / "index.html").read_text()
    assert "<svg" in html and "How the overall route depends on the threshold" in html
    assert "routing.yaml" in html and "10,000 tasks" in html
    assert 'href="../../runs/' in html  # rungs link to their published runs


def test_reexport_replaces_its_own_bundle_but_not_other_folders(bundle, states, tmp_path):
    out, _, ids = bundle
    ix = Index(ROOT, states[0])
    export_bundle(ROOT, ix, out, runs=[ids["run_ok"]])
    assert not (out / "sweeps").exists()
    foreign = tmp_path / "site"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("not ours")
    with pytest.raises(ExportRefused, match="didn't write"):
        export_bundle(ROOT, ix, foreign, runs=[ids["run_ok"]])
    assert (foreign / "keep.txt").exists()


def test_heldout_and_unfinished_runs_are_refused(states, tmp_path):
    ids = states[1]
    runs = tmp_path / "runs"
    shutil.copytree(states[0] / ids["run_ok"], runs / ids["run_ok"])
    m = json.loads((runs / ids["run_ok"] / "manifest.json").read_text())
    m["dataset"]["split"] = "heldout"
    (runs / ids["run_ok"] / "manifest.json").write_text(json.dumps(m))
    ix = Index(ROOT, runs)
    with pytest.raises(ExportRefused, match="held-out"):
        export_bundle(ROOT, ix, tmp_path / "out", runs=[ids["run_ok"]])
    with pytest.raises(ExportRefused, match="interrupted"):
        export_bundle(ROOT, Index(ROOT, states[0]), tmp_path / "out", runs=[ids["run_interrupted"]])
    assert not (tmp_path / "out").exists()


def test_denylist_hits_are_refused_without_echoing_the_term(states, tmp_path):
    ids = states[1]
    deny = tmp_path / "deny.txt"
    deny.write_text("# a term only this denylist knows\nfake-l[a]dder\n")
    with pytest.raises(ExportRefused) as e:
        export_bundle(ROOT, Index(ROOT, states[0]), tmp_path / "out", runs=[ids["run_ok"]], denylist=deny)
    assert "the private denylist matches run 1 of 1" in str(e.value) and "fake-ladder" not in str(e.value)
    assert not (tmp_path / "out").exists()


def test_cli_scorecard_is_the_same_static_page(states):
    html = (states[0] / states[1]["run_ok"] / "scorecard.html").read_text()
    assert "<script" not in html and 'class="chart interval"' in html and "Read before quoting" not in html
