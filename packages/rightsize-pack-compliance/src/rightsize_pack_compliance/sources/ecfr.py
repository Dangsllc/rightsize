"""Fetch a pinned edition of a CFR part from the eCFR versioner API and flatten it into
paragraph records with a canonical citation and a parent link.

The output is the benchmark's own copy of the regulation. Nothing here reads any system's
knowledge base, so the dataset never depends on a system under test.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from lxml import etree

ECFR_URL = "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-{title}.xml"

_MARKERS = re.compile(r"^\s*((?:\(\s*(?:[a-z]{1,2}|\d{1,3}|[ivxlc]+|[A-Z])\s*\)\s*)+)")
_ONE = re.compile(r"\(\s*([a-z]{1,2}|\d{1,3}|[ivxlc]+|[A-Z])\s*\)")
_ROMAN = re.compile(r"^[ivxlc]+$")
_ROMANS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii",
           "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx", "xxi", "xxii", "xxiii", "xxiv", "xxv"]


@dataclass
class Paragraph:
    control_code: str
    parent: str | None
    section: str
    part: str
    subpart: str | None
    section_title: str
    title: str
    text: str
    kind: str  # section | paragraph
    standard_type: str | None = None  # required | addressable | None
    is_standard: bool = False
    children: list[str] = field(default_factory=list)


def fetch(title: int, part: str, date: str, dest: Path) -> Path:
    """Download the part's XML (eCFR requires compression) and save it verbatim."""
    resp = httpx.get(
        ECFR_URL.format(date=date, title=title),
        params={"part": part},
        headers={"Accept-Encoding": "gzip, deflate"},
        timeout=120,
    )
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest


def _text(el: etree._Element) -> str:
    s = "".join(el.itertext())
    s = html.unescape(s).replace("—", "—")
    return re.sub(r"\s+", " ", s).strip()


def _next_lower(cur: str | None) -> str:
    if cur is None:
        return "a"
    if len(cur) == 1:
        return chr(ord(cur) + 1) if cur != "z" else "aa"
    return chr(ord(cur[0]) + 1) * 2


def _next_roman(cur: str | None) -> str:
    if cur is None:
        return "i"
    i = _ROMANS.index(cur) if cur in _ROMANS else -1
    return _ROMANS[i + 1] if 0 <= i < len(_ROMANS) - 1 else ""


_FIRST = {1: "a", 2: "1", 3: "i", 4: "A", 5: "1", 6: "i"}


def _next(level: int, cur: str) -> str:
    if level in (2, 5):
        return str(int(cur) + 1)
    if level in (3, 6):
        return _next_roman(cur)
    if level == 4:
        return chr(ord(cur) + 1)
    return _next_lower(cur)


def _candidates(tok: str) -> list[int]:
    if tok.isdigit():
        return [2, 5]
    if tok.isupper():
        return [4]
    out = []
    if len(tok) == 1 or (len(tok) == 2 and tok[0] == tok[1]):
        out.append(1)
    if _ROMAN.match(tok):
        out += [3, 6]
    return out


def _level_for(tok: str, stack: list[tuple[int, str]]) -> int:
    """Pick the hierarchy level for a marker. Levels: 1 (a), 2 (1), 3 (i), 4 (A), 5 (1), 6 (i).

    A marker either opens a new level directly below the current one (and must be that level's
    first value) or continues a level already on the stack (and must be the next value there).
    The deepest level that fits wins, which resolves "(i)" letter-vs-roman and "(2)" level-2
    vs level-5 the way the CFR uses them.
    """
    by_level = dict(stack)
    depth = stack[-1][0] if stack else 0
    cands = _candidates(tok)
    valid = []
    for lvl in cands:
        if lvl == depth + 1 and tok == _FIRST[lvl] or lvl in by_level and tok == _next(lvl, by_level[lvl]):
            valid.append(lvl)
    if valid:
        return max(valid)
    return min(cands) if cands else depth + 1


def _heading(p: etree._Element) -> str:
    i = p.find("I")
    if i is None:
        return ""
    return _text(i).rstrip(".:").rstrip(" (").strip()


def _push_markers(tokens, stack, section, subpart, sec_title, part, add):
    for tok in tokens:
        lvl = _level_for(tok, stack)
        stack = [(lv, v) for lv, v in stack if lv < lvl] + [(lvl, tok)]
        code = section + "".join(f"({v})" for _, v in stack)
        parent = section + "".join(f"({v})" for _, v in stack[:-1])
        add(Paragraph(code, parent, section, part, subpart, sec_title, "", "", "paragraph"))
    return stack


def _fill(rec: Paragraph, text: str, heading: str) -> None:
    rec.text = text
    rec.title = heading
    low = heading.lower()
    rec.is_standard = low.startswith("standard")
    head_zone = text.lower()[: len(heading) + 40]
    if "(required)" in head_zone or "required" in low:
        rec.standard_type = "required"
    if "(addressable)" in head_zone or "addressable" in low:
        rec.standard_type = "addressable"


def parse_part(xml_path: Path) -> list[Paragraph]:
    root = etree.parse(str(xml_path)).getroot()
    part = root.get("N")
    out: dict[str, Paragraph] = {}
    order: list[str] = []

    def add(rec: Paragraph) -> None:
        if rec.control_code in out:
            existing = out[rec.control_code]
            if rec.text and not existing.text:
                existing.text, existing.title = rec.text, rec.title or existing.title
            return
        out[rec.control_code] = rec
        order.append(rec.control_code)
        if rec.parent and rec.parent in out:
            out[rec.parent].children.append(rec.control_code)

    for sec in root.iter("DIV8"):
        if sec.get("TYPE") != "SECTION":
            continue
        section = sec.get("N")
        sub_el = sec.getparent()
        subpart = sub_el.get("N") if sub_el is not None and sub_el.get("TYPE") == "SUBPART" else None
        head = sec.find("HEAD")
        sec_title = re.sub(r"^§\s*[\d.]+\s*", "", _text(head)).rstrip(".") if head is not None else ""
        intro: list[str] = []
        add(Paragraph(section, None, section, part, subpart, sec_title, sec_title, "", "section"))
        stack: list[tuple[int, str]] = []
        for p in sec.findall("P"):
            text = _text(p)
            m = _MARKERS.match(text)
            if not m:
                if stack:
                    code = section + "".join(f"({v})" for _, v in stack)
                    out[code].text = (out[code].text + " " + text).strip()
                else:
                    intro.append(text)
                continue
            stack = _push_markers(_ONE.findall(m.group(1)), stack, section, subpart, sec_title, part, add)
            code = section + "".join(f"({v})" for _, v in stack)
            heading = _heading(p)
            _fill(out[code], text, heading)
            # Inline child right after the heading: "(b)(1) Standard: X. (i) Do this" or "...—(i) ..."
            i_el = p.find("I")
            tail = re.sub(r"^[\s\u2014-]+", "", html.unescape(i_el.tail or "")) if i_el is not None else ""
            tm = _MARKERS.match(tail)
            if heading and tm:
                stack = _push_markers(_ONE.findall(tm.group(1)), stack, section, subpart, sec_title, part, add)
                child = section + "".join(f"({v})" for _, v in stack)
                start = text.find(tm.group(1).strip(), text.find(heading) + len(heading))
                child_text = text[start:] if start >= 0 else tail
                inner = p.findall("I")
                _fill(out[child], child_text, _text(inner[1]).rstrip(".:") if len(inner) > 1 else "")
        out[section].text = " ".join(intro)
    return [out[c] for c in order]


def write_jsonl(paragraphs: list[Paragraph], dest: Path, edition: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as f:
        for p in paragraphs:
            row = asdict(p) | {"edition": edition}
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return dest


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
