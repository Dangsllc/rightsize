"""Find the Rightsize checkout and load its .env, the same way for the CLI, the Workbench, and jobs."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(root: Path) -> None:
    """Read KEY=VALUE lines from a gitignored .env at the repo root (never overrides real env)."""
    path = root / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))


def locate_root(start: Path | None = None) -> Path | None:
    """RIGHTSIZE_ROOT, else the nearest directory at or above `start` that has configs/suites/."""
    env = os.environ.get("RIGHTSIZE_ROOT")
    if env:
        return Path(env).resolve()
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / "configs" / "suites").is_dir():
            return cand
    return None


def env_present(name: str) -> bool:
    """Whether a variable is set to something non-empty. Never returns or logs the value."""
    return bool(os.environ.get(name))
