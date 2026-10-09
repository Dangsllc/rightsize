"""CFR citation handling: one canonical spelling, a hierarchy, and graded relevance.

Canonical form is ``164.312(a)(2)(iv)``: no "45 CFR", no "§", no spaces. Graded relevance
between a retrieved citation and a target:

* 3 exact
* 2 the hit is more specific than the target (a descendant)
* 1 the hit is less specific (an ancestor), or sits under the same standard
  (same section and top-level paragraph, e.g. 164.312(a)(1) vs 164.312(a)(2)(iv))
* 0 anything else, including unparseable citations
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PREFIX = re.compile(
    r"^\s*(?:\d{1,2}\s*c\.?\s*f\.?\s*r\.?|c\.?\s*f\.?\s*r\.?)?\s*(?:part\s*)?"
    r"(?:§+|sec(?:tion)?\.?)?\s*",
    re.IGNORECASE,
)
_SECTION = re.compile(r"^(\d{2,3})\s*\.\s*(\d{1,4})")
_PARA = re.compile(r"\s*\(\s*([A-Za-z]{1,4}|\d{1,3})\s*\)")


@dataclass(frozen=True)
class Citation:
    section: str  # e.g. "164.312"
    paragraphs: tuple[str, ...] = ()

    @property
    def canonical(self) -> str:
        return self.section + "".join(f"({p})" for p in self.paragraphs)

    @property
    def part(self) -> str:
        return self.section.split(".")[0]

    def is_ancestor_of(self, other: Citation) -> bool:
        return (
            self.section == other.section
            and len(self.paragraphs) < len(other.paragraphs)
            and other.paragraphs[: len(self.paragraphs)] == self.paragraphs
        )

    def parent(self) -> Citation | None:
        if self.paragraphs:
            return Citation(self.section, self.paragraphs[:-1])
        return None

    def __str__(self) -> str:
        return self.canonical


def parse(raw: str | None) -> Citation | None:
    """Parse any common spelling of a CFR citation. Returns None if there is no section number."""
    if not raw:
        return None
    text = _PREFIX.sub("", str(raw).strip())
    m = _SECTION.search(text)
    if not m:
        return None
    section = f"{m.group(1)}.{int(m.group(2))}"
    rest = text[m.end():]
    paras: list[str] = []
    pos = 0
    while (pm := _PARA.match(rest, pos)) is not None:
        paras.append(pm.group(1))
        pos = pm.end()
    return Citation(section, tuple(paras))


def normalize(raw: str | None) -> str | None:
    c = parse(raw)
    return c.canonical if c else None


def grade(hit: str | Citation | None, target: str | Citation) -> int:
    h = hit if isinstance(hit, Citation) else parse(hit)
    t = target if isinstance(target, Citation) else parse(target)
    if h is None or t is None:
        return 0
    if h == t:
        return 3
    if t.is_ancestor_of(h):
        return 2
    if h.is_ancestor_of(t):
        return 1
    if h.section == t.section and h.paragraphs[:1] and h.paragraphs[:1] == t.paragraphs[:1]:
        return 1
    return 0


def best_grade(hit: str | None, targets: list[str]) -> int:
    return max((grade(hit, t) for t in targets), default=0)


_CITATION_IN_TEXT = re.compile(
    r"(?:\d{1,2}\s*C\.?F\.?R\.?\s*(?:§+\s*)?|§+\s*)?\b(\d{2,3})\s*\.\s*(\d{1,4})((?:\s*\(\s*(?:[A-Za-z]{1,4}|\d{1,3})\s*\))*)"
)


def find_in_text(text: str) -> list[str]:
    """Pull canonical citations out of free text, in order of appearance, de-duplicated."""
    seen: list[str] = []
    for m in _CITATION_IN_TEXT.finditer(text or ""):
        c = normalize(f"{m.group(1)}.{m.group(2)}{m.group(3) or ''}")
        if c and c not in seen:
            seen.append(c)
    return seen
