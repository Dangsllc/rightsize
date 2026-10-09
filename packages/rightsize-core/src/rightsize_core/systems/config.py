"""Load a system YAML. `protocol: rightsize/v1` means the system speaks our protocol natively;
anything else is a declarative mapping onto the system's own API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from rightsize_core.systems.base import System
from rightsize_core.systems.declarative import DeclarativeSystem, expand_env


def _pack_transforms(pack_name: str | None) -> dict[str, Any]:
    if not pack_name:
        return {}
    from rightsize_core.packs import get_pack

    return getattr(get_pack(pack_name), "transforms", {})


def system_from_config(path: Path, root: Path) -> System:
    cfg = yaml.safe_load(path.read_text())
    if cfg.get("protocol") == "rightsize/v1":
        if cfg["transport"] == "http":
            from rightsize_core.systems.native import NativeHTTPSystem

            headers = {}
            if cfg.get("auth"):
                import os

                headers[cfg["auth"].get("name", "Authorization")] = os.environ.get(cfg["auth"]["env"], "")
            return NativeHTTPSystem(expand_env(cfg["base_url"]), headers=headers,
                                    trusted=bool(cfg.get("trusted")), name=cfg.get("name"))
        from rightsize_core.systems.native import NativeMCPSystem

        return NativeMCPSystem(expand_env(cfg["server"]), trusted=bool(cfg.get("trusted")),
                               env=expand_env(cfg.get("env", {})), name=cfg.get("name"))
    return DeclarativeSystem(cfg, transforms=_pack_transforms(cfg.get("pack", "compliance")))
