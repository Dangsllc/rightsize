from pathlib import Path

import pytest
from rightsize_core.calibration.sweep import load_ladder
from rightsize_core.systems.plugin import PluginSystem
from rightsize_reference_systems.llm import LLMSystem, check_not_truncated

ROOT = Path(__file__).resolve().parents[2]


def _bare(**kw) -> LLMSystem:
    s = LLMSystem.__new__(LLMSystem)  # skip loading the corpus: routing needs only these fields
    s.provider, s.base_url, s.api_key_env = kw.get("provider", "anthropic"), kw.get("base_url"), kw.get("api_key_env")
    s.skill_meta = {"skill": None, "sha256": "x"}
    return s


def _recorded(rung_name: str, monkeypatch) -> dict:
    """The self_report a run's manifest records (run.py writes system.describe()) for one rung of
    the shipped local-vs-api ladder, configured the way a sweep configures it."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-value")
    monkeypatch.setenv("LM_API_TOKEN", "test-lmstudio-token-value")
    rung = next(r for r in load_ladder(ROOT / "configs/ladders/local-vs-api.yaml") if r.name == rung_name)
    system = PluginSystem(_bare())  # constructor default provider: anthropic
    system.configure(rung.model, rung.params)
    return system.describe()["self_report"]


def test_manifest_records_the_gemini_route_not_the_default(monkeypatch):
    rep = _recorded("gemini-3.1-flash-lite", monkeypatch)
    assert rep["provider"] == "openai"  # Gemini's OpenAI-compatible endpoint
    assert rep["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert rep["api_key_env"] == "GEMINI_API_KEY"
    assert "test-gemini-key-value" not in repr(rep)  # the key's name, never its value


def test_manifest_records_the_lm_studio_route(monkeypatch):
    rep = _recorded("qwen2.5-7b@lmstudio", monkeypatch)
    assert rep["provider"] == "openai"
    assert rep["base_url"] == "http://127.0.0.1:1234/v1"
    assert rep["api_key_env"] == "LM_API_TOKEN"
    assert "test-lmstudio-token-value" not in repr(rep)


def test_default_route_names_the_anthropic_key_env():
    rep = _bare().self_report({})
    assert (rep["provider"], rep["base_url"], rep["api_key_env"]) == ("anthropic", None, "ANTHROPIC_API_KEY")


def test_plugins_whose_self_report_takes_no_params_still_work():
    class Old:
        def self_report(self):
            return {"provider": "x"}

    system = PluginSystem(Old())
    system.configure("m", {"provider": "y"})
    assert system.describe()["self_report"] == {"provider": "x"}


def test_rung_params_override_the_system_route():
    s = _bare()
    assert s.route({}) == ("anthropic", None, None)
    assert s.route({"provider": "openai", "base_url": "http://127.0.0.1:1234/v1", "api_key_env": "LM_API_TOKEN"}) == (
        "openai", "http://127.0.0.1:1234/v1", "LM_API_TOKEN")
    assert _bare(provider="ollama", base_url="http://h:11434").route({"num_ctx": 4096}) == ("ollama", "http://h:11434", None)


def test_truncation_guard():
    prompt = "x" * 60_000  # about 10k+ tokens by any tokenizer
    check_not_truncated("stop", 15_000, prompt)
    check_not_truncated("stop", None, prompt)  # no usage reported: nothing to check
    with pytest.raises(RuntimeError, match="truncating the context"):
        check_not_truncated("stop", 4096, prompt)
    with pytest.raises(RuntimeError, match="max_tokens"):
        check_not_truncated("length", 15_000, prompt)
