import json
from pathlib import Path

import yaml
from rightsize_core.cli import app
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]


def test_sweep_then_calibrate_picks_cheap_for_easy_and_large_for_hard(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGHTSIZE_PLUGIN_PATH", str(ROOT / "tests/fixtures"))
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    ladder = tmp_path / "ladder.yaml"
    ladder.write_text(yaml.safe_dump({"rungs": [{"name": "small", "model": "fake-small"},
                                                {"name": "large", "model": "fake-large"}]}))
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text(yaml.safe_dump({"as_of": "test", "models": {"fake-small": {"input": 0.1, "output": 0.4},
                                                                   "fake-large": {"input": 10, "output": 40}}}))
    runner = CliRunner()
    res = runner.invoke(app, ["sweep", "--system", "plugin:fake_ladder_system:make", "--ladder", str(ladder),
                              "--split", "public", "--task-type", "retrieval", "--pricing", str(pricing)])
    assert res.exit_code == 0, res.output
    sweep_dir = Path(next(ln for ln in res.output.splitlines() if ln.startswith("sweep_dir=")).split("=", 1)[1])
    try:
        out = tmp_path / "routing.yaml"
        res = runner.invoke(app, ["calibrate", str(sweep_dir), "--threshold", "recall@1=0.8", "--emit", str(out)])
        assert res.exit_code == 0, res.output
        cal = json.loads((sweep_dir / "calibration.json").read_text())
        pick = {d["stratum"]: d["chosen"] for d in cal["decisions"]}
        assert pick["query_style=exact_term"] == "small"
        if "query_style=scenario" in pick:  # present once LLM-written tiers are built
            assert pick["query_style=scenario"] == "large"
        routing = yaml.safe_load(out.read_text())
        assert "retrieval" in routing["routes"]
        assert (sweep_dir / "calibration.html").read_text().count("<svg") >= 1
    finally:
        import shutil

        for r in json.loads((sweep_dir / "sweep.json").read_text())["runs"]:
            shutil.rmtree(r["run_dir"], ignore_errors=True)
        shutil.rmtree(sweep_dir, ignore_errors=True)


def test_sweep_refuses_system_without_model_param(monkeypatch):
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    res = CliRunner().invoke(app, ["sweep", "--system", "ref:bm25", "--split", "public", "--rungs", "haiku-4.5",
                                   "--task-type", "retrieval"])
    assert res.exit_code != 0
    assert "can't be swept" in str(res.exception) or "can't be swept" in res.output


def test_unknown_cost_falls_back_to_ladder_order(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGHTSIZE_PLUGIN_PATH", str(ROOT / "tests/fixtures"))
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    ladder = tmp_path / "ladder.yaml"
    # "zz-small" sorts last alphabetically but is listed first (cheapest) in the ladder.
    ladder.write_text(yaml.safe_dump({"rungs": [
        {"name": "zz-small", "model": "fake-small", "params": {"hide_usage": True}},
        {"name": "aa-large", "model": "fake-large", "params": {"hide_usage": True}}]}))
    runner = CliRunner()
    res = runner.invoke(app, ["sweep", "--system", "plugin:fake_ladder_system:make", "--ladder", str(ladder),
                              "--split", "public", "--task-type", "retrieval"])
    assert res.exit_code == 0, res.output
    sweep_dir = Path(next(ln for ln in res.output.splitlines() if ln.startswith("sweep_dir=")).split("=", 1)[1])
    try:
        res = runner.invoke(app, ["calibrate", str(sweep_dir), "--threshold", "recall@1=0.8"])
        assert res.exit_code == 0, res.output
        cal = json.loads((sweep_dir / "calibration.json").read_text())
        d = next(d for d in cal["decisions"] if d["stratum"] == "query_style=exact_term")
        assert d["ordering"] == "ladder order (cost unknown)"
        assert d["chosen"] == "zz-small"
    finally:
        import shutil

        for r in json.loads((sweep_dir / "sweep.json").read_text())["runs"]:
            shutil.rmtree(r["run_dir"], ignore_errors=True)
        shutil.rmtree(sweep_dir, ignore_errors=True)


def test_ladder_rung_concurrency_is_optional(tmp_path):
    from rightsize_core.calibration.sweep import load_ladder

    ladder = tmp_path / "ladder.yaml"
    ladder.write_text(yaml.safe_dump({"rungs": [{"name": "a", "model": "m"},
                                                {"name": "b", "model": "m", "concurrency": 1, "params": {"x": 1}}]}))
    a, b = load_ladder(ladder)
    assert (a.concurrency, b.concurrency, b.params) == (None, 1, {"x": 1})


def test_shipped_ladders_load_and_are_priced():
    from rightsize_core.calibration.sweep import load_ladder
    from rightsize_core.usage.pricing import Pricing

    pricing = Pricing(ROOT / "configs/pricing/2026-10.yaml")
    for path in (ROOT / "configs/ladders").glob("*.yaml"):
        for rung in load_ladder(path):
            assert pricing.rate(rung.model) is not None, f"{path.name}: {rung.name} has no price on file"


def test_diff_fail_on_regression(monkeypatch):
    import shutil

    monkeypatch.setenv("RIGHTSIZE_PLUGIN_PATH", str(ROOT / "tests/fixtures"))
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    runner = CliRunner()
    dirs = {}
    try:
        for model in ("fake-large", "fake-small"):
            res = runner.invoke(app, ["run", "--system", "plugin:fake_ladder_system:make", "--model", model,
                                      "--task-type", "retrieval"])
            assert res.exit_code == 0, res.output
            dirs[model] = next(ln for ln in res.output.splitlines() if "scorecard.html" in ln).strip().split()[-1]
            dirs[model] = str(Path(dirs[model]).parent)
        worse = runner.invoke(app, ["diff", dirs["fake-large"], dirs["fake-small"], "--fail-on-regression"])
        assert worse.exit_code == 1, worse.output
        assert "REGRESSION" in worse.output
        better = runner.invoke(app, ["diff", dirs["fake-small"], dirs["fake-large"], "--fail-on-regression"])
        assert better.exit_code == 0, better.output
        plain = runner.invoke(app, ["diff", dirs["fake-large"], dirs["fake-small"]])
        assert plain.exit_code == 0
    finally:
        for d in dirs.values():
            shutil.rmtree(d, ignore_errors=True)
