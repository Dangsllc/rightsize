"""Turn usage records into dollars using a pinned pricing file. No usage means unknown cost."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from rightsize_core.schema import UsageRecord


class Pricing:
    def __init__(self, path: Path) -> None:
        self.path = path
        raw = path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        data = yaml.safe_load(raw)
        self.as_of = data.get("as_of")
        self.models: dict[str, dict] = data.get("models", {})

    def rate(self, model: str | None) -> dict | None:
        if not model:
            return None
        if model in self.models:
            return self.models[model]
        for key, val in self.models.items():  # tolerate provider prefixes like "anthropic:"
            if model.endswith(key) or key.endswith(model):
                return val
        return None

    def cost(self, usage: list[UsageRecord] | None) -> float | None:
        if not usage:
            return None
        total = 0.0
        for u in usage:
            r = self.rate(u.model)
            if r is None:
                return None
            total += u.input_tokens / 1e6 * r.get("input", 0.0)
            total += (u.output_tokens + (u.thinking_tokens or 0)) / 1e6 * r.get("output", 0.0)
            total += (u.cache_read_tokens or 0) / 1e6 * r.get("cache_read", r.get("input", 0.0))
        return total

    def describe(self) -> dict:
        return {"file": str(self.path), "sha256": self.sha256, "as_of": self.as_of}
