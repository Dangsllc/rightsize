"""Nothing published may depend on private material or describe a private target.

Public rules live here. Target-specific patterns live in `private/guard/denylist.txt` (one regex
per line) and are enforced whenever the private checkout is present, e.g. before every push.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()
DENYLIST = ROOT / "private/guard/denylist.txt"
SKIP_SUFFIXES = {".lock", ".png", ".jpg", ".ico", ".pdf"}
PUBLIC_RULES = [
    (re.compile(r"(?<![pw])rote(?!-compliance-skills)", re.IGNORECASE), set()),
    (re.compile(r"private/"), {".gitignore", "configs/suites/m1.yaml"}),
]


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        paths = [ROOT / p for p in out.decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "private", "runs"}
        paths = [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]
    return [p for p in paths if p.is_file() and p.resolve() != SELF and p.suffix not in SKIP_SUFFIXES]


def _offenders(rules) -> list[str]:
    found = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, allowed in rules:
            if rel not in allowed and (m := pattern.search(text)):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{rel}:{line}: {pattern.pattern}")
    return found


def test_no_private_references():
    offenders = _offenders(PUBLIC_RULES)
    assert not offenders, "Public files reference private material:\n" + "\n".join(offenders)


def test_private_denylist():
    if not DENYLIST.exists():
        pytest.skip("private checkout not present")
    lines = [ln.strip() for ln in DENYLIST.read_text().splitlines()]
    rules = [(re.compile(ln, re.IGNORECASE | re.MULTILINE), set()) for ln in lines if ln and not ln.startswith("#")]
    offenders = _offenders(rules)
    assert not offenders, "Public files match the private denylist:\n" + "\n".join(offenders)


def test_private_is_gitignored():
    gitignore = (ROOT / ".gitignore").read_text()
    assert re.search(r"^/?private/?$", gitignore, re.MULTILINE)
