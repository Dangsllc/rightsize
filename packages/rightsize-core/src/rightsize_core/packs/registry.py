from __future__ import annotations

from importlib.metadata import entry_points

from rightsize_core.packs.protocol import TaskPack

_cache: dict[str, TaskPack] = {}


def get_pack(name: str) -> TaskPack:
    if name not in _cache:
        for ep in entry_points(group="rightsize.packs"):
            if ep.name == name:
                _cache[name] = ep.load()
                break
        else:
            raise KeyError(f"No task pack named {name!r} is installed (entry point group rightsize.packs)")
    return _cache[name]


def available_packs() -> list[str]:
    return sorted(ep.name for ep in entry_points(group="rightsize.packs"))
