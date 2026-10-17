"""Check a system config before it touches the network, and say which line is wrong.

`validate_config` parses the YAML, checks its shape (typos in step or field keys are errors, not
silently ignored), and checks it against the task pack: extract fields must be fields of the
prediction contract, vocabulary maps must land on allowed values, and transforms must exist.
`locate` points a conformance finding at the config line most likely behind it. Validation is
advice for editors and agents; loading a config doesn't require it.
"""

from __future__ import annotations

import re
import typing
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_ENVVAR = re.compile(r"\$\{([A-Z0-9_]+)\}")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Until(_Strict):
    path: str
    in_: list[Any] = Field(alias="in")


class Poll(_Strict):
    call: str | None = None
    tool: str | None = None
    args: Any = None
    query: Any = None
    body: Any = None
    until: Until
    fail_on: list[Any] = []
    every_s: float = 5
    timeout_s: float = 1800

    @model_validator(mode="after")
    def one_target(self):
        if (self.call is None) == (self.tool is None):
            raise ValueError("a poll needs exactly one of `call` or `tool`")
        return self


class Step(_Strict):
    call: str | None = None
    tool: str | None = None
    poll: Poll | None = None
    set: dict[str, Any] | None = None
    body: Any = None
    query: Any = None
    multipart: dict[str, Any] | None = None
    form: Any = None
    args: Any = None
    save: dict[str, str] | None = None
    allow_error: bool = False

    @model_validator(mode="after")
    def one_action(self):
        kinds = [k for k in ("call", "tool", "poll", "set") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError("a step needs exactly one of `call`, `tool`, `poll`, or `set`"
                             + (f" (has {', '.join(kinds)})" if kinds else ""))
        if self.call is not None and not re.match(rf"^({'|'.join(_METHODS)})\s+\S", self.call.strip()):
            raise ValueError(f"`call` is METHOD /path, e.g. `POST /v1/search` (got {self.call!r})")
        for field, part in (self.multipart or {}).items():
            if isinstance(part, dict) and ("content" not in part or set(part) - {"content", "filename", "content_type"}):
                raise ValueError(f"multipart `{field}` takes content, filename, and content_type")
        return self


class FieldSpec(_Strict):
    path: str | None = None
    map: dict[Any, Any] | None = None
    default: Any = None
    const: Any = None
    transform: str | None = None
    type: Literal["float", "bool"] | None = None
    passthrough: bool = False
    allowed: list[Any] = []


class ListSpec(_Strict):
    each: str | None = None
    fields: dict[str, Any] | None = None
    from_text: str | None = None
    transform: str | None = None
    limit: int | None = None

    @model_validator(mode="after")
    def one_source(self):
        if self.each is None and self.from_text is None:
            raise ValueError("a list needs `each` (with `fields`) or `from_text` (with `transform`)")
        if self.each is not None and not self.fields:
            raise ValueError("`each` needs `fields`")
        if self.from_text is not None and not self.transform:
            raise ValueError("`from_text` needs `transform`")
        return self


class TaskSpec(_Strict):
    steps: list[Step]
    extract: dict[str, Any]
    extract_from: str = "last"
    cleanup: list[Step] = []
    binding: dict[str, Any] = {}
    path: str | None = None
    granularity: Literal["spec", "group", "document"] = "spec"


class Auth(_Strict):
    type: Literal["bearer", "header"] | None = None
    env: str
    name: str | None = None


class Throttle(_Strict):
    rps: float | None = None
    concurrency: int | None = None


class SystemConfig(_Strict):
    name: str
    transport: Literal["http", "mcp"]
    protocol: str | None = None
    base_url: str | None = None
    server: str | None = None
    env: dict[str, str] = {}
    auth: Auth | None = None
    headers: dict[str, str] = {}
    trusted: bool = False
    throttle: Throttle | None = None
    timeout_s: float | None = None
    model_param: Any = None
    models: list[str] = []
    version: str | None = None
    pack: str | None = None
    usage: ListSpec | None = None
    binding: dict[str, Any] = {}
    setup: list[Step] = []
    teardown: list[Step] = []
    healthcheck: Step | None = None
    tasks: dict[str, Any] = {}

    @model_validator(mode="after")
    def endpoint(self):
        if self.transport == "http" and not self.base_url and not self.server:
            raise ValueError("an http config needs `base_url`")
        if self.transport == "mcp" and not self.server:
            raise ValueError("an mcp config needs `server` (a command, or a URL)")
        if self.protocol != "rightsize/v1" and not self.tasks:
            raise ValueError("a declarative config needs `tasks` (or `protocol: rightsize/v1`)")
        return self


@dataclass
class Problem:
    line: int | None
    where: str
    message: str
    severity: Literal["error", "warning"] = "error"

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "where": self.where, "message": self.message, "severity": self.severity}


# ------------------------------------------------------------------------------ YAML lines


def _node_at(node, path: list) -> Any:
    for key in path:
        if isinstance(node, yaml.MappingNode):
            node = next((v for k, v in node.value if k.value == str(key)), None)
        elif isinstance(node, yaml.SequenceNode) and isinstance(key, int) and key < len(node.value):
            node = node.value[key]
        else:
            return None
        if node is None:
            return None
    return node


def line_of(text: str, path: list) -> int | None:
    """1-based line of the YAML node at `path`, or of its nearest existing ancestor."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    for cut in range(len(path), -1, -1):
        node = _node_at(root, path[:cut]) if root is not None else None
        if node is not None:
            return node.start_mark.line + 1
    return None


def _where(path: list) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "(top level)"


# ------------------------------------------------------------------------------ validation


def _item_model(annotation) -> type[BaseModel] | None:
    """The BaseModel inside list[X] (or X | None)."""
    for arg in typing.get_args(annotation) or ():
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
        inner = _item_model(arg)
        if inner is not None:
            return inner
    return None


def _literal_values(annotation) -> set | None:
    if typing.get_origin(annotation) is Literal:
        return set(typing.get_args(annotation))
    for arg in typing.get_args(annotation) or ():
        vals = _literal_values(arg)
        if vals is not None:
            return vals
    return None


def _check_fields(problems: list[Problem], text: str, base: list, fields: dict, model: type[BaseModel],
                  transforms: set[str]) -> None:
    known = model.model_fields
    for fname, fspec in fields.items():
        here = [*base, fname]
        if fname not in known:
            problems.append(Problem(line_of(text, here), _where(here),
                                    f"`{fname}` isn't part of the contract; expected one of {', '.join(known)}"))
            continue
        if not isinstance(fspec, dict):
            continue
        if fspec.get("transform") and fspec["transform"] not in transforms:
            problems.append(Problem(line_of(text, [*here, "transform"]), _where([*here, "transform"]),
                                    f"no transform called {fspec['transform']!r}; available: {', '.join(sorted(transforms))}"))
        allowed = _literal_values(known[fname].annotation)
        if allowed and isinstance(fspec.get("map"), dict):
            bad = sorted({str(v) for v in fspec["map"].values() if v not in allowed})
            if bad:
                problems.append(Problem(line_of(text, [*here, "map"]), _where([*here, "map"]),
                                        f"maps to {', '.join(bad)}, which isn't allowed; use {', '.join(sorted(allowed))}"))


def validate_config(text: str, root=None, pack_name: str | None = None) -> list[Problem]:
    """Every problem found, each with the line it's on. An empty list means the config is well formed
    (it says nothing yet about whether the system's answers map; conformance checks that)."""
    from rightsize_core.service.env import env_present

    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        msg = f"not valid YAML: {getattr(e, 'problem', e)}"
        if "expected ',' or '}'" in msg or "expected ',' or ']'" in msg:
            msg += ". Inside {...}, a JSONPath with [ or ] needs quotes: {path: \"$.items[*]\"}"
        return [Problem(mark.line + 1 if mark else None, "(yaml)", msg)]
    if not isinstance(cfg, dict):
        return [Problem(1, "(top level)", "a config is a YAML mapping (name:, transport:, tasks: ...)")]
    problems: list[Problem] = []

    def check(model: type[BaseModel], data: Any, base: list) -> None:
        try:
            model.model_validate(data)
        except ValidationError as e:
            for err in e.errors():
                loc = [*base, *err["loc"]]
                msg = err["msg"].removeprefix("Value error, ")
                if err["type"] == "extra_forbidden":
                    msg = f"unknown key `{loc[-1]}` (a typo?)"
                problems.append(Problem(line_of(text, loc), _where(loc), msg))

    def check_field(spec: Any, base: list) -> None:
        if not isinstance(spec, str):
            check(FieldSpec, spec, base)

    check(SystemConfig, cfg, [])
    for tt, tspec in (cfg.get("tasks") or {}).items() if isinstance(cfg.get("tasks"), dict) else ():
        base = ["tasks", tt]
        if tspec == "unsupported":
            continue
        if not isinstance(tspec, dict):
            problems.append(Problem(line_of(text, base), _where(base),
                                    "a task is `unsupported`, or a mapping with `steps` and `extract`"))
            continue
        check(TaskSpec, tspec, base)
        for key, spec in (tspec.get("extract") or {}).items() if isinstance(tspec.get("extract"), dict) else ():
            here = [*base, "extract", key]
            if isinstance(spec, dict) and ("each" in spec or "from_text" in spec):
                check(ListSpec, spec, here)
                for fname, fspec in (spec.get("fields") or {}).items() if isinstance(spec.get("fields"), dict) else ():
                    check_field(fspec, [*here, "fields", fname])
            else:
                check_field(spec, here)
    # Problems that need the task pack.
    try:
        from rightsize_core.packs import get_pack

        pack = get_pack(pack_name or cfg.get("pack") or "compliance")
    except KeyError:
        pack = None
    if pack is not None and isinstance(cfg.get("tasks"), dict):
        transforms = set(getattr(pack, "transforms", {}))
        for tt, tspec in cfg["tasks"].items():
            if tt not in pack.task_types:
                problems.append(Problem(line_of(text, ["tasks", tt]), f"tasks.{tt}",
                                        f"no task type {tt!r}; this pack has {', '.join(pack.task_types)}"))
                continue
            if not isinstance(tspec, dict) or not isinstance(tspec.get("extract"), dict):
                continue
            pred = pack.task_types[tt].prediction_model
            for key, spec in tspec["extract"].items():
                here = ["tasks", tt, "extract", key]
                if key not in pred.model_fields:
                    problems.append(Problem(line_of(text, here), _where(here), f"`{key}` isn't part of the "
                                            f"{tt} prediction; expected {', '.join(pred.model_fields)}"))
                    continue
                item = _item_model(pred.model_fields[key].annotation)
                if item is not None and isinstance(spec, dict) and isinstance(spec.get("fields"), dict):
                    _check_fields(problems, text, [*here, "fields"], spec["fields"], item, transforms)
                if isinstance(spec, dict) and spec.get("transform") and spec["transform"] not in transforms:
                    problems.append(Problem(line_of(text, [*here, "transform"]), _where([*here, "transform"]),
                                            f"no transform called {spec['transform']!r}"))
    for i, line in enumerate(text.splitlines(), 1):
        for var in _ENVVAR.findall(line):
            if not env_present(var):
                problems.append(Problem(i, "(environment)", f"{var} isn't set; add it to .env", "warning"))
    auth_env = (cfg.get("auth") or {}).get("env") if isinstance(cfg.get("auth"), dict) else None
    if auth_env and not env_present(auth_env):
        problems.append(Problem(line_of(text, ["auth", "env"]), "auth.env", f"{auth_env} isn't set; add it to .env",
                                "warning"))
    if cfg.get("trusted") is True:
        problems.append(Problem(line_of(text, ["trusted"]), "trusted", "trusted systems receive the held-out split; "
                                "only set this for a system you run yourself", "warning"))
    return sorted(problems, key=lambda p: (p.line or 0, p.severity != "error"))


# ------------------------------------------------------------------------------ findings -> lines


def locate(text: str, task_type: str | None, message: str) -> int | None:
    """The config line most likely behind a conformance finding."""
    try:
        cfg = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if m := _ENVVAR.search(message) or re.search(r"Environment variable (\w+)|Auth env var (\w+)", message):
        var = next(g for g in m.groups() if g)
        for i, line in enumerate(text.splitlines(), 1):
            if var in line:
                return i
    tspec = (cfg.get("tasks") or {}).get(task_type) if task_type else None
    if not isinstance(tspec, dict):
        return line_of(text, ["tasks", task_type]) if task_type else None
    extract = tspec.get("extract") or {}
    if m := re.search(r"values with no mapping: (\w+)=", message):
        for key, spec in extract.items():
            if isinstance(spec, dict) and m.group(1) in (spec.get("fields") or {}):
                return line_of(text, ["tasks", task_type, "extract", key, "fields", m.group(1), "map"])
    if "does not match contract" in message:
        for key, spec in extract.items():
            for fname in (spec.get("fields") or {}) if isinstance(spec, dict) else {}:
                if f"'{fname}'" in message:
                    return line_of(text, ["tasks", task_type, "extract", key, "fields", fname])
        return line_of(text, ["tasks", task_type, "extract"])
    if "below" in message and "extract" in message:
        return line_of(text, ["tasks", task_type, "extract"])
    if m := re.search(r"\b(GET|POST|PUT|PATCH|DELETE) (/[^\s?]*)", message):
        method, path = m.groups()
        first = path.strip("/").split("/")[0]
        for i, step in enumerate(tspec.get("steps") or []):
            call = (step.get("call") or (step.get("poll") or {}).get("call") or "") if isinstance(step, dict) else ""
            if call.upper().startswith(method) and first in call:
                return line_of(text, ["tasks", task_type, "steps", i])
    if "poll" in message:
        for i, step in enumerate(tspec.get("steps") or []):
            if isinstance(step, dict) and "poll" in step:
                return line_of(text, ["tasks", task_type, "steps", i])
    return line_of(text, ["tasks", task_type, "steps"])
