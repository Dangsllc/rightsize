"""Minimal LLM backends used only to write frozen seed artifacts (never at scoring time).

`openai` covers any OpenAI-compatible server (Ollama's /v1, LM Studio, vLLM). `anthropic` uses
the official SDK. The model id that produced each seed is recorded next to it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx


@dataclass
class SeedLLM:
    backend: str  # "openai" | "anthropic"
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float | None = 0.7

    @property
    def label(self) -> str:
        return f"{self.backend}:{self.model}"

    def complete(self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
        if self.backend == "anthropic":
            import anthropic

            client = anthropic.Anthropic()
            kwargs = {}
            if self.model.startswith("claude-haiku-4-5") and self.temperature is not None:
                kwargs["extra_body"] = {"temperature": self.temperature}  # SDK 1.x: not a named arg
            with client.messages.stream(
                model=self.model, max_tokens=max(max_tokens, 16000), system=system,
                messages=[{"role": "user", "content": user}], **kwargs,
            ) as stream:
                msg = stream.get_final_message()
            if msg.stop_reason == "refusal":
                raise RuntimeError("model refused")
            return "".join(b.text for b in msg.content if b.type == "text")
        headers = {}
        key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(f"{self.base_url.rstrip('/')}/chat/completions", json=body, headers=headers, timeout=600)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def items_of(value) -> list:
    """Accept either a bare JSON list or an object wrapping one (`{"items": [...]}`)."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for v in value.values():
            if isinstance(v, list):
                return v
    raise ValueError("expected a JSON list")


def parse_json_block(text: str):
    """Pull the first JSON value out of a model reply (tolerates code fences and preambles)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON in reply")
    return json.JSONDecoder().raw_decode(text[start:])[0]
