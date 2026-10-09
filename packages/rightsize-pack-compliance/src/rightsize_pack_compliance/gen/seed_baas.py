"""Frozen base business associate agreements (suite m2): seed format, writer brief, checker.

A base is one fictional covered entity / business associate pair, written once in two
presentations that carry the same requirement content:

* ``template``      a standalone BAA in the shape of the HHS sample provisions
* ``vendor_paper``  the business associate's own paper: a services agreement whose BAA terms
                    sit in an exhibit, lean on defined terms and cross-references, and share the
                    page with ordinary commercial clauses

Each presentation is an ordered list of blocks (articles, clauses, paragraphs) made of sentences.
A sentence that meets a requirement element is tagged ``<unit>#<element>``; every element also
has a "weakened" twin that sounds alike but no longer meets it. The tags never reach a system
under test: they let the deterministic builder delete or weaken one named element in both
presentations and know exactly what label that produces.

Contracts are binding by default and grant permissions with "may", so the test for "this
sentence states the element" differs from the policy-manual test in ``seed_manuals``: a sentence
meets an element when it matches one of the element's signatures and states a binding term.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

PROMPT_VERSION = "baa-seed-v1"
PRESENTATIONS = ("template", "vendor_paper")
KINDS = ("article", "clause", "para")
WORDS = {"template": (1300, 4200), "vendor_paper": (2800, 6000)}

_BINDING = re.compile(
    r"\b(must|shall|will|agrees?|undertakes?|is (required|obligated)|are (required|obligated)|"
    r"may (only|not)|may terminate|authori[sz]es|(is|are) (entitled|authori[sz]ed) to|right to|"
    r"has the right|solely|only (as|to|for|in))\b",
    re.IGNORECASE,
)
_DEFINITIONAL = re.compile(r"\b(shall|will) (generally )?(have the (same )?meaning|mean|be defined)\b", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"under review|reserved|intentionally (left )?blank|to be (determined|completed|provided)|\bTBD\b|"
    r"future revision|\[[^\]]*\]",
    re.IGNORECASE,
)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def load_elements(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def units_of(elements: dict) -> dict[str, dict]:
    return {u["code"]: u for g in elements["groups"] for u in g["units"]}


def tags_of(elements: dict) -> dict[str, dict]:
    """Every element keyed by its tag, ``<unit code>#<element id>``."""
    return {f"{u['code']}#{e['id']}": e for u in units_of(elements).values() for e in u["elements"]}


def base_seed_path(root: Path, suite_cfg: dict, base: dict) -> Path:
    """Held-out bases are stored privately; everything else under data/."""
    if base.get("heldout"):
        from rightsize_core.integrity.dataset import heldout_dir

        return heldout_dir(root, suite_cfg).parent / "seeds" / "baas" / f"{base['id']}.v1.json"
    return root / suite_cfg["control_classification"]["seeds_dir"] / f"{base['id']}.v1.json"


def sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text or "") if s.strip()]


def binding(sentence: str) -> bool:
    return bool(_BINDING.search(_DEFINITIONAL.sub(" ", sentence)))


def meets(text, element: dict) -> bool:
    """True if some sentence of `text` states the element as a binding term."""
    if not isinstance(text, str):
        return False
    sigs = [re.compile(s, re.IGNORECASE) for s in element["signature"]]
    return any(binding(s) and any(p.search(s) for p in sigs) for s in sentences(text))


def word_count(pres: dict) -> int:
    n = len((pres.get("title") or "").split())
    for b in pres.get("blocks", []):
        n += len((b.get("heading") or "").split())
        n += sum(len(s.get("text", "").split()) for s in b.get("sentences", []) if isinstance(s, dict))
    return n


def check_presentation(pres: dict, elements: dict, name: str) -> list[str]:
    """Every problem with one presentation of a base. Empty means it is usable."""
    if not isinstance(pres, dict) or not isinstance(pres.get("blocks"), list):
        return [f"{name}: needs an object with a blocks list"]
    tags = tags_of(elements)
    unit_of_tag = {t: t.split("#", 1)[0] for t in tags}
    problems: list[str] = []
    if not isinstance(pres.get("title"), str) or not pres["title"].strip():
        problems.append(f"{name}: needs a title")
    tagged: dict[str, list[str]] = {t: [] for t in tags}
    for bi, b in enumerate(pres["blocks"]):
        where = f"{name} block {bi} ({b.get('heading') or b.get('kind')})" if isinstance(b, dict) else f"{name} block {bi}"
        if not isinstance(b, dict) or b.get("kind") not in KINDS:
            problems.append(f"{where}: kind must be one of {KINDS}")
            continue
        sents = b.get("sentences") or []
        if b.get("kind") != "article" and not sents:
            problems.append(f"{where}: no sentences")
        has_untagged = False
        for s in sents:
            if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
                problems.append(f"{where}: every sentence needs non-empty text")
                continue
            tag, text = s.get("tag"), s["text"]
            if _PLACEHOLDER.search(text):
                problems.append(f"{where}: placeholder or bracketed text: {text[:100]!r}")
            if len(sentences(text)) > 1:
                problems.append(f"{where}: one sentence per entry, split this: {text[:100]!r}")
            if tag in (None, "", "null"):
                has_untagged = True
            elif tag not in tags:
                problems.append(f"{where}: unknown tag {tag!r}")
                continue
            else:
                tagged[tag].append(text)
                if not meets(text, tags[tag]):
                    problems.append(f"{where}: sentence tagged {tag} does not meet it: {text[:140]!r}")
            for other, e in tags.items():
                if other != tag and meets(text, e):
                    problems.append(f"{where}: sentence {('tagged ' + tag) if tag else 'untagged'} also meets "
                                    f"{other}: {text[:140]!r}")
        if b.get("number") and not has_untagged:
            problems.append(f"{where}: a block with a fixed number needs an untagged sentence so it never empties")
    for tag, texts in tagged.items():
        if not texts:
            problems.append(f"{name}: no sentence tagged {tag}")
    weakened = pres.get("weakened") or {}
    for tag, e in tags.items():
        w = weakened.get(tag)
        if not isinstance(w, str) or not w.strip():
            problems.append(f"{name}: no weakened twin for {tag}")
            continue
        if len(sentences(w)) > 1:
            problems.append(f"{name}: weakened twin for {tag} must be one sentence")
        if meets(w, e):
            problems.append(f"{name}: weakened twin still meets {tag}: {w[:140]!r}")
        for other, oe in tags.items():
            if unit_of_tag[other] != unit_of_tag[tag] and meets(w, oe):
                problems.append(f"{name}: weakened twin for {tag} meets {other}: {w[:140]!r}")
        if _PLACEHOLDER.search(w):
            problems.append(f"{name}: placeholder or bracketed text in twin for {tag}")
    lo, hi = WORDS[name]
    n = word_count(pres)
    if not lo <= n <= hi:
        problems.append(f"{name}: {n} words; aim for {lo}-{hi}")
    return problems


def check_seed(seed: dict, elements: dict) -> list[str]:
    problems = []
    for key in ("base", "profile", "parties", "derivation", "generator_llm"):
        if not seed.get(key):
            problems.append(f"missing {key}")
    if seed.get("derivation") not in ("hhs_verbatim", "paraphrased"):
        problems.append("derivation must be hhs_verbatim or paraphrased")
    pres = seed.get("presentations") or {}
    for name in PRESENTATIONS:
        if name not in pres:
            problems.append(f"missing presentation {name}")
        else:
            problems += check_presentation(pres[name], elements, name)
    return problems


def save(path: Path, seed: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seed, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def brief(base: dict, elements: dict, hhs_text: str) -> str:
    """The writing instructions for one base. Seed writers (people or agents) follow this and
    iterate with `rightsize data check-baa` until it reports no problems."""
    lines = []
    for g in elements["groups"]:
        lines.append(f"\n### {g['topic']}")
        for u in g["units"]:
            lines.append(f"- {u['code']}  {u['title']}")
            for e in u["elements"]:
                lines.append(f"  - tag `{u['code']}#{e['id']}`: {' '.join(e['requires'].split())}")
    derivation = (
        "**hhs_verbatim**: the `template` presentation follows the HHS sample's wording closely, "
        "with the bracketed options filled in for this pair and bracketed instructions removed. "
        "Split HHS list items so each obligation is its own sentence with its own binding verb "
        "(\"Business Associate shall ...\"), since the lead-in \"agrees to:\" is a separate sentence."
        if base.get("derivation") == "hhs_verbatim" else
        "**paraphrased**: the `template` presentation follows the HHS sample's structure and "
        "covers the same ground, in this organization's own drafting voice. Do not copy HHS sentences."
    )
    return f"""# Writing brief: base `{base['id']}` (prompt {PROMPT_VERSION})

{' '.join(base['profile'].split())}

Write one business associate agreement for this pair, twice: a `template` presentation and a
`vendor_paper` presentation. Both must contain every requirement element below, each met by its
own tagged sentence, and a weakened twin for every element. The builder later removes or weakens
elements to make labelled variants, so write every tagged sentence so it can be deleted or swapped
without breaking the sentences around it (no "Such notice shall..." that leans on a tagged
sentence; no sentence that only makes sense next to another).

Derivation: {derivation}

## The two presentations
- `template` ({WORDS['template'][0]}-{WORDS['template'][1]} words): a standalone BAA in the HHS
  shape: parties and recitals, definitions, obligations of the business associate, permitted uses
  and disclosures, covered entity obligations, term and termination, miscellaneous, signatures.
  Use `article` blocks for the sections and `clause` blocks for the lettered items. Leave clause
  blocks unnumbered; the renderer letters them (a), (b), ... and re-letters when one is removed.
  Do not cross-reference clause letters.
- `vendor_paper` ({WORDS['vendor_paper'][0]}-{WORDS['vendor_paper'][1]} words): the business
  associate's own services agreement with the BAA terms in an exhibit. Make it read like real
  vendor paper:
  - obligations that work through defined terms ("Customer Data", "Security Incident" as defined
    in Section 1) and cross-references ("as set forth in Section 7.3");
  - an order-of-precedence clause;
  - at least two requirement elements stated in the services agreement body instead of the exhibit;
  - ordinary commercial clauses as untagged content: fees, service levels, warranties, indemnity,
    limitation of liability, insurance, data ownership, offshore or location limits, de-identified
    data rights, governing law, notices.
  Give vendor_paper blocks explicit `number`s ("7.3") so cross-references stay valid; every block
  with a number must keep at least one untagged sentence so it never disappears.

## Rules for sentences
- One sentence per entry. A tagged sentence states its element as a binding term ("shall",
  "will", "agrees to", "may only", "may not"; termination rights may say "may terminate").
- A sentence may meet only the element it is tagged with. Untagged sentences (definitions,
  recitals, commercial terms) must meet none. Definitions use "means" or "shall mean".
- Weakened twin: one sentence that drops in for the tagged sentence and reads naturally, but no
  longer meets the element. Weaken the substance, not just the modal verb, because contracts are
  binding by default. Realistic weakenings: a 90-day or open-ended breach deadline; reporting
  "Breaches" but not security incidents; flow-down only to "material" subcontractors or on
  "substantially similar" terms; "commercially reasonable security" with no Security Rule
  commitment; records "upon request" with no tie to the access/amendment/accounting rules; books
  and records available to the customer instead of the Secretary; retaining archival copies under
  a "standard retention schedule" with no continuing protections. A twin must not meet any other
  unit's element.
- No placeholders, brackets, "reserved", or "TBD". An absent requirement is simply absent.
- Cite regulation sections only where the HHS sample does or real contracts commonly do. Keep
  party names consistent with `parties`.
- Signatures (regexes) decide whether a sentence meets an element; read them in
  `data/seeds/baa_elements.v1.yaml` when the checker disagrees with you.

## Requirement elements (tag = `<unit>#<element>`)
{chr(10).join(lines)}

## Output
A JSON file at the path given to you, shaped:
{{"base": "{base['id']}", "profile": "...", "derivation": "{base.get('derivation', 'paraphrased')}",
  "generator_llm": "claude-code-agent:<model id>", "prompt_version": "{PROMPT_VERSION}",
  "elements_version": {elements['version']},
  "parties": {{"covered_entity": "...", "business_associate": "...", "relationship": "..."}},
  "presentations": {{
    "template": {{"title": "...", "blocks": [
        {{"kind": "para" | "article" | "clause", "number": null | "7.3", "heading": "..." | null,
          "sentences": [{{"text": "...", "tag": null | "164.504(e)(2)(ii)(C)#E1"}}]}}],
      "weakened": {{"<tag>": "<one sentence>"}}}},
    "vendor_paper": {{...same shape...}}}}}}

Check it with `uv run rightsize data check-baa --suite m2 --base {base['id']}` and fix every
problem it lists. Done means zero problems.

## The HHS sample provisions (structural baseline)
{hhs_text}
"""
