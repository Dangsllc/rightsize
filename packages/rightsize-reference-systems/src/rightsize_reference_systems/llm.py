"""A plain-LLM reference system: one model, one prompt, no retrieval tricks.

* control_classification: the whole document plus the list of units goes in one call; the model
  returns a status per unit (`mode=spec`) or per requirement group (`mode=group`, which pairs with
  systems that answer per group).
* retrieval: bm25 proposes candidates from the benchmark's own corpus; the model reranks them.

The system prompt is the public `control-assessment` SKILL.md from rote-compliance-skills when
`RIGHTSIZE_SKILLS_DIR` points at a checkout (its path and SHA are recorded), else a short neutral prompt.

Server-side model fallbacks are deliberately NOT enabled: a fallback would silently answer with a
different model than the one being measured. A refusal is recorded as its own error class.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from rightsize_core.schema import Outcome, OutcomeError, SystemInfo, UsageRecord

from rightsize_reference_systems.base import ReferenceSystem

NEUTRAL_PROMPT = (
    "You are a careful healthcare compliance analyst. Assess documents strictly against the "
    "requirement text you are given. A requirement is covered only if the document states it as a "
    "binding obligation; partial if some but not all required elements are present or the language "
    "is vague or optional; gap if it is absent."
)

STATUS = ["covered", "partial", "gap", "not_applicable"]


def _assessment_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "control_code": {"type": "string"},
                        "status": {"type": "string", "enum": STATUS},
                        "confidence": {"type": "number"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["control_code", "status", "confidence", "evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["assessments"],
        "additionalProperties": False,
    }


def _rerank_schema() -> dict:
    return {
        "type": "object",
        "properties": {"citations": {"type": "array", "items": {"type": "string"}}},
        "required": ["citations"],
        "additionalProperties": False,
    }


class LLMSystem(ReferenceSystem):
    def __init__(self, root: Path, sources: list[Path], provider: str | None = None,
                 base_url: str | None = None, api_key_env: str | None = None,
                 skill: str = "control-assessment", candidates: int = 30, **_: Any) -> None:
        self.provider = provider or os.environ.get("RIGHTSIZE_LLM_PROVIDER", "anthropic")
        self.base_url = base_url or os.environ.get("RIGHTSIZE_LLM_BASE_URL")
        self.api_key_env = api_key_env or os.environ.get("RIGHTSIZE_LLM_API_KEY_ENV")
        self.candidates = candidates
        self.skill_name = skill
        self.system_prompt, self.skill_meta = self._load_skill(skill)
        from rightsize_reference_systems.bm25 import BM25System

        self.bm25 = BM25System(sources)
        self.titles = {d["citation"]: d["text"][:400] for d in self.bm25.docs}

    # ------------------------------------------------------------------ setup
    def _load_skill(self, name: str) -> tuple[str, dict]:
        base = os.environ.get("RIGHTSIZE_SKILLS_DIR")
        if base:
            path = Path(base) / name / "SKILL.md"
            if path.exists():
                text = path.read_text(encoding="utf-8")
                sha = None
                try:
                    sha = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "HEAD"],
                                         capture_output=True, text=True, check=True).stdout.strip()
                except Exception:  # noqa: BLE001
                    pass
                return text, {"skill": name, "path": str(path), "git_sha": sha,
                              "sha256": hashlib.sha256(text.encode()).hexdigest()}
            raise FileNotFoundError(f"RIGHTSIZE_SKILLS_DIR is set but {path} does not exist")
        return NEUTRAL_PROMPT, {"skill": None, "sha256": hashlib.sha256(NEUTRAL_PROMPT.encode()).hexdigest()}

    def info(self) -> SystemInfo:
        return SystemInfo(
            name="llm", version="0.1.0", task_types=["retrieval", "control_classification"],
            granularity={"retrieval": "spec", "control_classification": "spec"},  # "group" with params.mode=group
            supports_model_param=True, reports_usage=True,
            efforts=["low", "medium", "high", "xhigh", "max"],
        )

    def self_report(self) -> dict:
        return {"provider": self.provider, "base_url": self.base_url, "prompt": self.skill_meta,
                "fallbacks": "disabled"}

    # ------------------------------------------------------------------ calls
    async def _call(self, model: str, system: str, user: str, schema: dict, params: dict) -> tuple[dict, list[UsageRecord], dict]:
        t0 = time.monotonic()
        if self.provider == "anthropic":
            import anthropic

            client = anthropic.AsyncAnthropic()
            kwargs: dict[str, Any] = {
                "model": model, "max_tokens": int(params.get("max_tokens", 32000)), "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            oc: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
            if params.get("effort") and not model.startswith("claude-haiku-4-5"):
                oc["effort"] = params["effort"]
            kwargs["output_config"] = oc
            if model.startswith("claude-haiku-4-5") and "temperature" in params:
                # SDK 1.x dropped the argument; models that still honour it take it via extra_body.
                kwargs["extra_body"] = {"temperature": float(params["temperature"])}
            async with client.messages.stream(**kwargs) as stream:
                msg = await stream.get_final_message()
            sent = {k: v for k, v in kwargs.items() if k not in ("system", "messages")}
            if msg.stop_reason == "refusal":
                raise RefusalError(getattr(getattr(msg, "stop_details", None), "category", None) or "refusal")
            text = next(b.text for b in msg.content if b.type == "text")
            u = msg.usage
            usage = [UsageRecord(provider="anthropic", model=msg.model, input_tokens=u.input_tokens,
                                 output_tokens=u.output_tokens,
                                 cache_read_tokens=getattr(u, "cache_read_input_tokens", None),
                                 latency_ms=int((time.monotonic() - t0) * 1000), call_role="assess")]
            return json.loads(text), usage, sent
        if self.provider == "ollama":
            # Native API, so the context window is explicit. Ollama's OpenAI-compatible endpoint
            # silently truncates prompts to its default context (often 4096 tokens).
            num_ctx = int(params.get("num_ctx", 32768))
            body = {"model": model, "stream": False, "format": schema,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "options": {"num_ctx": num_ctx, "temperature": float(params.get("temperature", 0.0))}}
            async with httpx.AsyncClient(timeout=3600) as c:
                r = await c.post(f"{(self.base_url or 'http://127.0.0.1:11434').rstrip('/')}/api/chat", json=body)
                r.raise_for_status()
                data = r.json()
            from rightsize_pack_compliance.gen.llm import parse_json_block

            n_in = data.get("prompt_eval_count", 0)
            if n_in >= num_ctx:
                raise RuntimeError(f"prompt filled the whole {num_ctx}-token context; raise params.num_ctx")
            usage = [UsageRecord(provider="ollama", model=data.get("model", model), input_tokens=n_in,
                                 output_tokens=data.get("eval_count", 0),
                                 latency_ms=int((time.monotonic() - t0) * 1000), call_role="assess")]
            sent = {"model": model, "options": body["options"], "format": "json_schema"}
            return parse_json_block(data["message"]["content"]), usage, sent
        # OpenAI-compatible (LM Studio, vLLM)
        headers = {}
        if self.api_key_env and os.environ.get(self.api_key_env):
            headers["Authorization"] = f"Bearer {os.environ[self.api_key_env]}"
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user + "\n\nReturn only JSON matching: " + json.dumps(schema)}],
            "response_format": {"type": "json_object"},
            "temperature": float(params.get("temperature", 0.0)),
            "max_tokens": int(params.get("max_tokens", 8000)),
        }
        async with httpx.AsyncClient(timeout=1800) as c:
            r = await c.post(f"{self.base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        from rightsize_pack_compliance.gen.llm import parse_json_block

        text = data["choices"][0]["message"]["content"]
        u = data.get("usage") or {}
        usage = [UsageRecord(provider="openai-compatible", model=data.get("model", model),
                             input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0),
                             estimated=not u, latency_ms=int((time.monotonic() - t0) * 1000), call_role="assess")]
        sent = {k: v for k, v in body.items() if k != "messages"}
        return parse_json_block(text), usage, sent

    # ------------------------------------------------------------------ tasks
    async def run(self, task_type: str, inputs: dict[str, Any], model: str | None,
                  params: dict[str, Any]) -> Outcome:
        if not model:
            return Outcome(error=OutcomeError(kind="error", message="llm reference system needs --model"))
        try:
            if task_type == "control_classification":
                return await self._classify(inputs, model, params)
            if task_type == "retrieval":
                return await self._retrieve(inputs, model, params)
        except RefusalError as e:
            return Outcome(error=OutcomeError(kind="refusal", message=str(e)))
        return Outcome(error=OutcomeError(kind="unsupported", message=task_type))

    async def _classify(self, inputs: dict, model: str, params: dict) -> Outcome:
        mode = params.get("mode", "spec")
        if mode == "group":
            groups = inputs.get("groups") or {}
            by_code = {u["control_code"]: u for u in inputs["units"]}
            items = []
            for gcode, members in groups.items():
                body = "\n".join(f"    - {m}: {by_code[m]['specification']}" for m in members if m in by_code)
                items.append(f"- {gcode} (requirement group with these implementation specifications):\n{body}")
            what = ("requirement group. A group is covered only if all its specifications are met, gap if "
                    "none are, partial otherwise")
        else:
            items = [f"- {u['control_code']}: {u['title']}. {u['specification']}" for u in inputs["units"]]
            what = "requirement"
        user = (
            f"Assess the policy document below against each {what}. Use exactly the codes given.\n\n"
            "Requirements:\n" + "\n".join(items)
            + "\n\nFor each, return status (covered, partial, gap, or not_applicable), a confidence between 0 and 1, "
            "and a short evidence quote from the document (empty if none).\n\n"
            f"<document>\n{inputs['document']}\n</document>"
        )
        data, usage, sent = await self._call(model, self.system_prompt, user, _assessment_schema(), params)
        sent["mode"] = mode
        return Outcome(prediction={"assessments": data.get("assessments", [])}, usage=usage,
                       params_sent=sent, latency_kind="model", path=f"llm-{mode}")

    async def _retrieve(self, inputs: dict, model: str, params: dict) -> Outcome:
        k = int(inputs.get("k", 10))
        cand = await self.bm25.run("retrieval", {"query": inputs["query"], "k": self.candidates}, None, {})
        cites = [h["citation"] for h in cand.prediction["hits"]]
        listing = "\n".join(f"- {c}: {self.titles.get(c, '')}" for c in cites)
        user = (
            f"Question: {inputs['query']}\n\nCandidate provisions of 45 CFR Parts 160 and 164:\n{listing}\n\n"
            f"Return the {k} candidate citations that best answer the question, best first, using the codes exactly as given."
        )
        data, usage, sent = await self._call(model, NEUTRAL_PROMPT, user, _rerank_schema(), params)
        ranked = [c for c in data.get("citations", []) if c in cites][:k]
        ranked += [c for c in cites if c not in ranked][: max(0, k - len(ranked))]
        hits = [{"citation": c, "score": float(k - i)} for i, c in enumerate(ranked)]
        return Outcome(prediction={"hits": hits}, usage=usage, params_sent=sent, latency_kind="model",
                       path="bm25+llm-rerank")


class RefusalError(RuntimeError):
    pass
