"""Turn a `--system` argument into a System.

* http(s)://host      a native rightsize/v1 HTTP server
* mcp+http(s)://host  a native rightsize/v1 MCP server over streamable HTTP
* mcp+stdio:<cmd>     a native rightsize/v1 MCP server started as a subprocess
* ref:<name>          a reference system in-process (bm25, dense, llm, oracle)
* plugin:<mod:attr>   any in-process object with info() and run()
* path/to/file.yaml   a system config (native or declarative)
"""

from __future__ import annotations

from pathlib import Path

from rightsize_core.systems.base import System


def load_system(spec: str, root: Path, suite: str = "m1") -> System:
    if spec.startswith(("http://", "https://")):
        from rightsize_core.systems.native import NativeHTTPSystem

        return NativeHTTPSystem(spec)
    if spec.startswith("mcp+"):
        from rightsize_core.systems.native import NativeMCPSystem

        target = spec[len("mcp+"):]
        target = target.removeprefix("stdio:")
        return NativeMCPSystem(target)
    if spec.startswith("ref:"):
        from rightsize_reference_systems.registry import make

        from rightsize_core.systems.plugin import PluginSystem

        name, _, arg = spec[4:].partition("@")
        kwargs = {"split": arg} if name == "oracle" and arg else {}
        return PluginSystem(make(name, root, suite, **kwargs), name=name)
    if spec.startswith("plugin:"):
        from rightsize_core.systems.plugin import PluginSystem, load_object

        return PluginSystem(load_object(spec[len("plugin:"):]))
    path = Path(spec)
    if path.suffix in (".yaml", ".yml"):
        from rightsize_core.systems.config import system_from_config

        return system_from_config(path, root)
    raise ValueError(f"Don't know how to reach system {spec!r}")
