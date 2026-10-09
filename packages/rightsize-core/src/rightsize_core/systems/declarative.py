"""Reach any system's own HTTP or MCP API through a YAML config: no code on the system's side.

A config describes, per task type, a list of steps (HTTP calls, MCP tool calls, polling), how to
pull the prediction out of the responses with JSONPath, and how to translate the system's
vocabulary (e.g. "non_compliant") into the pack's (e.g. "gap"). See configs/systems/examples/.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from typing import Any

import httpx
from jinja2 import Environment, StrictUndefined
from jsonpath_ng.ext import parse as jp_parse

from rightsize_core.schema import Outcome, OutcomeError, SystemInfo, UsageRecord
from rightsize_core.systems.base import System, SystemPreflightError

_ENV = Environment(undefined=StrictUndefined)
_WHOLE = re.compile(r"^\s*\{\{\s*(.+?)\s*\}\}\s*$")
_ENVVAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


class StepError(RuntimeError):
    pass


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            if m.group(1) not in os.environ:
                raise SystemPreflightError(f"Environment variable {m.group(1)} is not set")
            return os.environ[m.group(1)]
        return _ENVVAR.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Render templates in strings. A string that is exactly one {{ expr }} keeps its type."""
    if isinstance(value, str):
        m = _WHOLE.match(value)
        if m:
            return _ENV.compile_expression(m.group(1), undefined_to_none=False)(**ctx)
        if "{{" in value or "{%" in value:
            return _ENV.from_string(value).render(**ctx)
        return value
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def jp_all(path: str, data: Any) -> list[Any]:
    if path in ("$", "@"):
        return [data]
    return [m.value for m in jp_parse(path).find(data)]


def jp_first(path: str, data: Any, default: Any = None) -> Any:
    vals = jp_all(path, data)
    return vals[0] if vals else default


class Extractor:
    """Builds a prediction dict from a response using a small declarative spec.

    A field spec is either a JSONPath string, or a dict with `path`, optional `map` (value
    translation), `default`, `const`, `transform` (a pack-registered function), and `type`.
    A list spec has `each` (JSONPath to items) and `fields`, or `from_text` + `transform`.
    """

    def __init__(self, transforms: dict[str, Any] | None = None) -> None:
        self.transforms = transforms or {}
        self.unmapped: list[tuple[str, Any]] = []

    def field(self, name: str, spec: Any, item: Any) -> Any:
        if isinstance(spec, str):
            return jp_first(spec, item)
        if "const" in spec:
            return spec["const"]
        val = jp_first(spec["path"], item, spec.get("default")) if "path" in spec else item
        if "map" in spec and val is not None:
            mapping = spec["map"]
            key = str(val).strip()
            if key in mapping:
                val = mapping[key]
            elif key.lower() in {str(k).lower() for k in mapping}:
                val = next(v for k, v in mapping.items() if str(k).lower() == key.lower())
            elif spec.get("passthrough", False) and key in spec.get("allowed", []):
                val = key
            else:
                self.unmapped.append((name, val))
                val = None
        if "transform" in spec and val is not None:
            val = self.transforms[spec["transform"]](val)
        if spec.get("type") == "float" and val is not None:
            try:
                val = float(val)
            except (TypeError, ValueError):
                val = None
        if spec.get("type") == "bool" and val is not None:
            val = bool(val) if not isinstance(val, str) else val.lower() in ("true", "1", "yes")
        return val

    def list_(self, spec: dict, data: Any) -> list[dict]:
        if "from_text" in spec:
            text = jp_first(spec["from_text"], data, "")
            return self.transforms[spec["transform"]](text or "")
        items = jp_all(spec["each"], data)
        out = []
        for it in items:
            row = {k: self.field(k, fs, it) for k, fs in spec["fields"].items()}
            out.append({k: v for k, v in row.items() if v is not None})
        return out[: spec["limit"]] if "limit" in spec else out

    def build(self, spec: dict, data: Any) -> dict:
        pred = {}
        for key, s in spec.items():
            pred[key] = self.list_(s, data) if isinstance(s, dict) and ("each" in s or "from_text" in s) else self.field(key, s, data)
        return pred


class DeclarativeSystem(System):
    declarative = True

    def __init__(self, cfg: dict[str, Any], transforms: dict[str, Any] | None = None,
                 mcp_server_override: Any = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.name = cfg["name"]
        self.transport = f"declarative-{cfg['transport']}"
        self.trusted = bool(cfg.get("trusted", False))
        self.transforms = transforms or {}
        self.run_ctx: dict[str, Any] = {}
        thr = cfg.get("throttle", {})
        self._sem = asyncio.Semaphore(int(thr.get("concurrency", 4)))
        self._min_gap = 1.0 / float(thr["rps"]) if thr.get("rps") else 0.0
        self._last = 0.0
        self._rate_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self._mcp = None
        self._mcp_override = mcp_server_override

    # ------------------------------------------------------------- connection
    def _headers(self) -> dict[str, str]:
        h = dict(expand_env(self.cfg.get("headers", {})))
        auth = self.cfg.get("auth")
        if auth:
            val = os.environ.get(auth["env"])
            if not val:
                raise SystemPreflightError(f"Auth env var {auth['env']} is not set")
            if auth.get("type") == "bearer":
                h["Authorization"] = f"Bearer {val}"
            else:
                h[auth.get("name", "X-API-Key")] = val
        return h

    async def _ensure(self) -> None:
        # A config may mix transports (e.g. upload over REST, analyze over MCP): open whichever
        # endpoints it names.
        if self.cfg.get("base_url") and self._http is None:
            self._http = httpx.AsyncClient(
                base_url=expand_env(self.cfg["base_url"]), headers=self._headers(),
                timeout=httpx.Timeout(float(self.cfg.get("timeout_s", 600)), connect=15),
            )
        if (self.cfg.get("server") or self._mcp_override is not None) and self._mcp is None:
            from rightsize_core.systems.mcp_client import MCPConnection

            server = self._mcp_override or expand_env(self.cfg["server"])
            self._mcp = MCPConnection(server, env=expand_env(self.cfg.get("env", {})),
                                      headers=self._headers() or None)
            await self._mcp.start()

    async def _throttle(self) -> None:
        if not self._min_gap:
            return
        async with self._rate_lock:
            wait = self._last + self._min_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    # ------------------------------------------------------------- protocol
    async def info(self) -> SystemInfo:
        tasks = [t for t, spec in self.cfg.get("tasks", {}).items() if spec not in (None, "unsupported")]
        return SystemInfo(
            name=self.name, version=self.cfg.get("version"), protocol="rightsize/v1 (declarative)",
            task_types=tasks,
            granularity={t: s.get("granularity", "spec") for t, s in self.cfg["tasks"].items() if isinstance(s, dict)},
            supports_model_param=bool(self.cfg.get("model_param")),
            models=self.cfg.get("models", []),
            reports_usage=bool(self.cfg.get("usage")),
        )

    async def preflight(self) -> None:
        await self._ensure()
        hc = self.cfg.get("healthcheck")
        if hc:
            try:
                await self._step(hc, dict(self.run_ctx))
            except Exception as e:
                raise SystemPreflightError(f"healthcheck failed: {e}") from e

    async def setup(self) -> None:
        await self._ensure()
        ctx = self._base_ctx({})
        for step in self.cfg.get("setup", []):
            await self._step(step, ctx)
        self.run_ctx = {k: v for k, v in ctx.items() if k not in ("inputs", "model", "params", "binding", "env")}

    async def teardown(self) -> None:
        ctx = self._base_ctx({})
        for step in self.cfg.get("teardown", []):
            try:
                await self._step(step, ctx)
            except Exception:  # noqa: BLE001 - teardown is best effort, but recorded by the caller
                pass

    def _base_ctx(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {**self.run_ctx, "inputs": inputs, "model": self.model, "params": self.params,
                "binding": self.cfg.get("binding", {}), "env": {}}

    async def run(self, task_type: str, inputs: dict[str, Any]) -> Outcome:
        tcfg = self.cfg["tasks"].get(task_type)
        if not isinstance(tcfg, dict):
            return Outcome(error=OutcomeError(kind="unsupported", message=task_type))
        ctx = self._base_ctx(inputs)
        ctx["binding"] = {**ctx["binding"], **tcfg.get("binding", {})}
        t0 = time.monotonic()
        async with self._sem:
            try:
                for step in tcfg["steps"]:
                    await self._step(step, ctx)
                ex = Extractor(self.transforms)
                data = ctx.get(tcfg.get("extract_from", "last"))
                prediction = ex.build(copy.deepcopy(tcfg["extract"]), data)
                usage = self._usage(ctx) if self.cfg.get("usage") else None
                out = Outcome(
                    prediction=prediction, usage=usage,
                    params_sent={"model": self.model, **self.params} if self.cfg.get("model_param") else {},
                    latency_ms=int((time.monotonic() - t0) * 1000), latency_kind="end_to_end",
                    path=tcfg.get("path", self.name),
                    raw={"unmapped": ex.unmapped, "response": data},
                )
            except Exception as e:  # noqa: BLE001
                out = Outcome(error=OutcomeError(kind="error", message=f"{type(e).__name__}: {e}"[:800]),
                              latency_ms=int((time.monotonic() - t0) * 1000), path=tcfg.get("path", self.name))
            finally:
                for step in tcfg.get("cleanup", []):
                    try:
                        await self._step(step, ctx)
                    except Exception:  # noqa: BLE001
                        pass
        return out

    def _usage(self, ctx: dict) -> list[UsageRecord] | None:
        spec = self.cfg["usage"]
        data = ctx.get(spec.get("from", "last"))
        rows = Extractor().list_(spec, data)
        return [UsageRecord(**{k: v for k, v in r.items() if k in UsageRecord.model_fields}) for r in rows] or None

    # ------------------------------------------------------------- steps
    async def _step(self, step: dict, ctx: dict) -> Any:
        if "call" in step:
            data = await self._http_call(step, ctx)
        elif "tool" in step:
            data = await self._mcp_call(step, ctx)
        elif "poll" in step:
            data = await self._poll(step, ctx)
        elif "set" in step:
            data = render(step["set"], ctx)
            ctx.update(data)
            return data
        else:
            raise StepError(f"Unknown step {step}")
        ctx["last"] = data
        for var, path in (step.get("save") or {}).items():
            ctx[var] = jp_first(path, data)
        return data

    async def _http_call(self, step: dict, ctx: dict) -> Any:
        await self._ensure()
        method, _, path = render(step["call"], ctx).partition(" ")
        kwargs: dict[str, Any] = {}
        if "body" in step:
            body = render(step["body"], ctx)
            if self.model is None and isinstance(body, dict):
                body = {k: v for k, v in body.items() if v is not None}
            kwargs["json"] = body
        if "query" in step:
            kwargs["params"] = render(step["query"], ctx)
        if "multipart" in step:
            files = {}
            for field, spec in step["multipart"].items():
                spec = spec if isinstance(spec, dict) else {"content": spec}
                content = render(spec["content"], ctx)
                fname = render(spec.get("filename", "document.md"), ctx)
                files[field] = (fname, content.encode() if isinstance(content, str) else content,
                                spec.get("content_type", "text/markdown"))
            kwargs["files"] = files
            if "form" in step:
                kwargs["data"] = render(step["form"], ctx)
        await self._throttle()
        r = await self._http.request(method.upper(), path, **kwargs)
        if r.status_code >= 400 and not step.get("allow_error"):
            raise StepError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except (json.JSONDecodeError, ValueError):
            return r.text

    async def _mcp_call(self, step: dict, ctx: dict) -> Any:
        await self._ensure()
        await self._throttle()
        return await self._mcp.call(render(step["tool"], ctx), render(step.get("args", {}), ctx))

    async def _poll(self, step: dict, ctx: dict) -> Any:
        spec = step["poll"]
        every = float(spec.get("every_s", 5))
        deadline = time.monotonic() + float(spec.get("timeout_s", 1800))
        inner = {k: v for k, v in spec.items() if k in ("call", "tool", "args", "query", "body")}
        while True:
            data = await (self._http_call(inner, ctx) if "call" in inner else self._mcp_call(inner, ctx))
            val = jp_first(spec["until"]["path"], data)
            if "fail_on" in spec and val in spec["fail_on"]:
                raise StepError(f"poll reached failure state {val!r}")
            if val in spec["until"]["in"]:
                return data
            if time.monotonic() > deadline:
                raise StepError(f"poll timed out waiting for {spec['until']} (last {val!r})")
            await asyncio.sleep(every)

    def describe(self) -> dict[str, Any]:
        import hashlib

        return {
            **super().describe(),
            "config_sha256": hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest(),
            "endpoint": self.cfg.get("base_url") or self.cfg.get("server"),
        }

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        if self._mcp is not None:
            await self._mcp.close()
