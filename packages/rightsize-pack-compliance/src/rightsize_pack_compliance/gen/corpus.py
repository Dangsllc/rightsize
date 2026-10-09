"""The benchmark's view of the regulation: paragraph records keyed by canonical citation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rightsize_pack_compliance.gen.text import clean_title
from rightsize_pack_compliance.sources.ecfr import load_jsonl


@dataclass
class Corpus:
    rows: dict[str, dict]

    @classmethod
    def load(cls, paths: list[Path]) -> Corpus:
        rows: dict[str, dict] = {}
        for p in paths:
            for r in load_jsonl(p):
                rows[r["control_code"]] = r
        return cls(rows)

    def __contains__(self, code: str) -> bool:
        return code in self.rows

    def subtree_text(self, code: str) -> str:
        r = self.rows[code]
        parts = [r["text"]]
        for child in r.get("children", []):
            parts.append(self.subtree_text(child))
        return " ".join(p for p in parts if p)

    def display_title(self, code: str) -> str:
        r = self.rows[code]
        t = clean_title(r["title"] or "")
        if not t or t.lower() in {"standard", "standards", "implementation specifications"}:
            parent = r.get("parent")
            while parent and not clean_title(self.rows[parent]["title"] or ""):
                parent = self.rows[parent].get("parent")
            t = clean_title(self.rows[parent]["title"]) if parent else ""
            if not t or t.lower() in {"standard", "standards"}:
                t = r["section_title"]
        return t

    def security_rule_specs(self) -> list[str]:
        return [
            c for c, r in self.rows.items()
            if r["part"] == "164" and r["subpart"] == "C"
            and ("(Required)" in (r["title"] or "") or "(Addressable)" in (r["title"] or ""))
        ]

    def standard_of(self, code: str) -> str | None:
        """The 'Standard:' paragraph a spec belongs to (its nearest standard ancestor or sibling)."""
        r = self.rows[code]
        if r.get("is_standard"):
            return code
        parent = r.get("parent")
        while parent:
            pr = self.rows[parent]
            if pr.get("is_standard"):
                return parent
            for sib in pr.get("children", []):
                if self.rows[sib].get("is_standard"):
                    return sib
            parent = pr.get("parent")
        return None
