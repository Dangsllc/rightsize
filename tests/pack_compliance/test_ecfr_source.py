"""Checks on the committed eCFR snapshot (no network)."""

from pathlib import Path

import pytest
from rightsize_pack_compliance.sources.ecfr import load_jsonl

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data/sources/ecfr_45cfr164@2026-09-29.jsonl"


@pytest.fixture(scope="module")
def part164():
    return {r["control_code"]: r for r in load_jsonl(SRC)}


def test_security_rule_specs(part164):
    specs = [
        r for r in part164.values()
        if r["subpart"] == "C" and ("(Required)" in r["title"] or "(Addressable)" in r["title"])
    ]
    assert len(specs) == 41
    assert all(r["standard_type"] in ("required", "addressable") for r in specs)


@pytest.mark.parametrize(
    "code,parent,title",
    [
        ("164.312(a)(2)(iv)", "164.312(a)(2)", "Encryption and decryption (Addressable)"),
        ("164.312(e)(2)(ii)", "164.312(e)(2)", "Encryption (Addressable)"),
        ("164.316(b)(2)(i)", "164.316(b)(2)", "Time limit (Required)"),
        ("164.316(b)(1)(ii)", "164.316(b)(1)", ""),
        ("164.308(a)(7)(ii)(A)", "164.308(a)(7)(ii)", "Data backup plan (Required)"),
    ],
)
def test_hierarchy(part164, code, parent, title):
    r = part164[code]
    assert r["parent"] == parent
    assert r["title"] == title


def test_standards_flagged(part164):
    assert part164["164.312(b)"]["is_standard"]
    assert part164["164.312(a)(1)"]["is_standard"]
    assert not part164["164.312(a)(2)(iv)"]["is_standard"]


def test_every_parent_exists(part164):
    missing = [c for c, r in part164.items() if r["parent"] and r["parent"] not in part164]
    assert not missing
