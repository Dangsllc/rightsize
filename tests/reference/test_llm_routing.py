import pytest
from rightsize_reference_systems.llm import LLMSystem, check_not_truncated


def _bare(**kw) -> LLMSystem:
    s = LLMSystem.__new__(LLMSystem)  # skip loading the corpus: routing needs only these fields
    s.provider, s.base_url, s.api_key_env = kw.get("provider", "anthropic"), kw.get("base_url"), kw.get("api_key_env")
    return s


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
