"""A published bundle must be exactly what `rightsize export` wrote: every file it lists still
hashes the same (no hand edits after the guards ran), and it holds no held-out run."""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Bundles live under results/ or docs/ (GitHub Pages serves a repo's root or its docs/ folder).
RECORDS = sorted(r for d in ("results", "docs") if (ROOT / d).is_dir() for r in (ROOT / d).rglob("export.json"))


@pytest.mark.parametrize("record", RECORDS, ids=[str(r.parent.relative_to(ROOT)) for r in RECORDS])
def test_published_bundle_matches_its_export_record(record):
    data = json.loads(record.read_text())
    assert all(r["split"] != "heldout" for r in data["runs"])
    for path, sha in data["files"].items():
        assert hashlib.sha256((record.parent / path).read_text().encode()).hexdigest() == sha, path
