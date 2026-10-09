"""Write the frozen base policy manuals. Run once per base; review; commit.

Each base manual is written group by group. Every requirement element gets its own tagged
sentence(s) plus a "weakened" twin that sounds similar but no longer meets the element. The
tags never reach a system under test; they let the deterministic builder delete or weaken one
named element and know exactly what label that produces.

Every group reply is checked before it is accepted:
* each element has a tagged sentence matching the element's signature
* no weakened twin matches the signature
* no untagged sentence (and no group intro) matches any member element's signature
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

from rightsize_pack_compliance.gen.corpus import Corpus
from rightsize_pack_compliance.gen.llm import SeedLLM, parse_json_block

log = logging.getLogger(__name__)
PROMPT_VERSION = "manual-seed-v1"

SYSTEM = (
    "You write realistic internal security policy manuals for US healthcare organizations. "
    "You never cite regulations or section numbers, and you never mention HIPAA rule titles."
)


def base_seed_path(root: Path, suite_cfg: dict, base: dict) -> Path:
    """Held-out bases are stored privately; everything else under data/."""
    if base.get("heldout"):
        from rightsize_core.integrity.dataset import heldout_dir

        return heldout_dir(root, suite_cfg).parent / "seeds" / "manuals" / f"{base['id']}.v1.json"
    return root / suite_cfg["control_classification"]["seeds_dir"] / f"{base['id']}.v1.json"


def load_elements(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _sig(element: dict) -> list[re.Pattern]:
    return [re.compile(s, re.IGNORECASE) for s in element["signature"]]


_HEDGE = re.compile(
    r"\b(may|might|could|consider(s|ed|ing)?|encourag\w*|where (possible|feasible|practical)|"
    r"as appropriate|when practical|if (possible|feasible)|at (its|their|the) discretion|optional(ly)?|"
    r"aims? to|strives? to|tr(y|ies) to|recommended|suggest\w*|ideally|generally|from time to time|should)\b",
    re.IGNORECASE,
)
_BINDING = re.compile(r"\b(must|shall|will|required|requires|is responsible|are responsible)\b", re.IGNORECASE)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text or "") if s.strip()]


def hedged(sentence: str) -> bool:
    """Optional or aspirational wording ("may consider", "are encouraged to") that states no
    obligation. A sentence with a binding modal ("must", "shall") is never hedged."""
    return bool(_HEDGE.search(sentence)) and not _BINDING.search(sentence)


def meets(text, element: dict) -> bool:
    """True if some sentence of `text` states the element as an obligation: it matches one of
    the element's signatures and is not hedged."""
    if not isinstance(text, str):
        return False
    sigs = _sig(element)
    return any(any(p.search(s) for p in sigs) and not hedged(s) for s in sentences(text))


matches = meets  # back-compat name used by the builder


def _group_prompt(base: dict, group: dict, corpus: Corpus) -> str:
    units = []
    for u in group["units"]:
        els = "\n".join(f"      {e['id']}: {e['requires']}" for e in u["elements"])
        units.append(f"  - unit {u['code']} (topic: {corpus.display_title(u['code'])})\n    elements:\n{els}")
    return f"""Organization: {base['profile']}

Write one chapter of this organization's security policy manual about: {group['topic']}.
The chapter has one subsection per unit below. For each unit:
- Give the subsection a short, natural heading in the organization's own words. Do not reuse the
  topic wording verbatim and do not cite any law or regulation.
- Write 3 to 6 sentences. Each element must be met by exactly one sentence that states a concrete,
  binding requirement (who does what, how, how often). Tag that sentence with the element id.
- Other sentences (context, roles, scope) are tagged null and must NOT meet any element by
  themselves.
- Write every tagged sentence as a binding obligation (use "must", "shall", or a plain
  statement of what the organization does).
- For every element, also write a "weakened" sentence: a drop-in replacement for the tagged
  sentence that sounds similar but no longer meets the element. Either make it optional
  ("may", "is encouraged to", "where feasible") or drop the specific requirement (no mechanism,
  period, or action).
- Untagged sentences and the chapter intro must not state any of the elements as obligations.
- Keep units distinct: never meet one unit's elements inside another unit's subsection.

Also write a one- or two-sentence chapter intro that states no requirements.

Units:
{chr(10).join(units)}

Return only JSON:
{{"heading": "...", "intro": "...",
  "sections": [{{"unit": "<unit code>", "heading": "...",
                 "sentences": [{{"text": "...", "element": "E1"}}, {{"text": "...", "element": null}}],
                 "weakened": {{"E1": "..."}}}}]}}"""


def _check_group(reply: dict, group: dict) -> list[str]:
    if not isinstance(reply, dict) or not isinstance(reply.get("sections"), list):
        return ["reply is not an object with a sections list"]
    problems = []
    if not isinstance(reply.get("heading"), str) or not isinstance(reply.get("intro"), str):
        problems.append("chapter heading and intro must be strings")
    for sec in reply["sections"]:
        if not isinstance(sec, dict) or not isinstance(sec.get("heading"), str):
            problems.append("every section needs a string heading")
            continue
        for s in sec.get("sentences") or []:
            if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
                problems.append(f"{sec.get('unit')}: every sentence needs non-empty text")
    by_unit = {s.get("unit"): s for s in reply.get("sections", [])}
    all_elements = [e for u in group["units"] for e in u["elements"]]
    for e in all_elements:
        if matches(reply.get("intro", ""), e):
            problems.append(f"intro meets {e['id']}")
    for u in group["units"]:
        sec = by_unit.get(u["code"])
        if not sec:
            problems.append(f"missing section {u['code']}")
            continue
        sents = [x for x in (sec.get("sentences") or []) if isinstance(x, dict)]
        for e in u["elements"]:
            tagged = [s["text"] for s in sents if s.get("element") == e["id"]]
            if not tagged:
                problems.append(f"{u['code']} {e['id']}: no tagged sentence")
            elif not any(matches(t, e) for t in tagged):
                problems.append(f"{u['code']} {e['id']}: tagged sentence misses signature: {tagged[0][:120]!r}")
            weak = (sec.get("weakened") or {}).get(e["id"])
            if not weak:
                problems.append(f"{u['code']} {e['id']}: no weakened twin")
            elif matches(weak, e):
                problems.append(f"{u['code']} {e['id']}: weakened twin still meets the element: {weak[:120]!r}")
        for s in sents:
            if s.get("element") in (None, "null", ""):
                for e in u["elements"]:
                    if matches(s["text"], e):
                        problems.append(f"{u['code']}: untagged sentence meets {e['id']}: {s['text'][:120]!r}")
    return problems


def _front_prompt(base: dict, n_distractors: int) -> str:
    return f"""Organization: {base['profile']}

Write the non-security parts of this organization's policy manual. Return only JSON:
{{"title": "<manual title>",
  "preamble": ["<purpose and scope paragraph>", "<roles and responsibilities paragraph>"],
  "distractors": [{{"heading": "...", "body": "..."}}],
  "near_misses": [{{"heading": "...", "body": "..."}}]}}

- "distractors": {n_distractors + 2} sections on unrelated administrative topics (for example
  leave, expense reimbursement, dress code, parking, social media, records of visitor badges),
  3 to 5 sentences each.
- "near_misses": 3 sections that talk about security topics (such as encryption, backups, or
  passwords) only in aspirational, non-binding language ("staff are encouraged to be aware..."):
  no required actions, no responsible roles, no frequencies, no specific mechanisms.
- Do not cite any law or regulation."""


def generate(root: Path, corpus: Corpus, suite_cfg: dict, llm: SeedLLM, base_id: str | None = None,
             max_attempts: int = 4) -> Path:
    ccfg = suite_cfg["control_classification"]
    elements = load_elements(root / ccfg["elements"])
    out_dir = root / ccfg["seeds_dir"]
    bases = [b for b in ccfg["bases"] if base_id in (None, b["id"])]
    if not bases:
        raise ValueError(f"No base named {base_id!r}")
    for base in bases:
        path = base_seed_path(root, suite_cfg, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        seed = json.loads(path.read_text()) if path.exists() else {
            "base": base["id"], "profile": base["profile"], "generator_llm": llm.label,
            "prompt_version": PROMPT_VERSION, "elements_version": elements["version"], "groups": {},
        }
        seed["canary"] = suite_cfg["canary"]
        if seed.get("generator_llm") != llm.label:
            log.warning("base %s was started by %s; continuing with %s", base["id"], seed["generator_llm"], llm.label)
        if "front" not in seed:
            for attempt in range(max_attempts):
                try:
                    front = parse_json_block(llm.complete(SYSTEM, _front_prompt(base, ccfg["distractors_per_manual"]),
                                                          max_tokens=6000, json_mode=True))
                    if front.get("distractors") and front.get("near_misses") and front.get("preamble"):
                        seed["front"] = front
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("front matter attempt %d: %s", attempt + 1, e)
            _save(path, seed)
        for group in elements["groups"]:
            if group["code"] in seed["groups"]:
                continue
            problems: list[str] = []
            for attempt in range(max_attempts):
                prompt = _group_prompt(base, group, corpus)
                if problems:
                    prompt += "\n\nYour previous answer had these problems; fix them:\n- " + "\n- ".join(problems[:10])
                try:
                    reply = parse_json_block(llm.complete(SYSTEM, prompt, max_tokens=8000, json_mode=True))
                except Exception as e:  # noqa: BLE001
                    problems = [f"invalid JSON: {e}"]
                    continue
                try:
                    problems = _check_group(reply, group)
                except Exception as e:  # noqa: BLE001 - a malformed reply is a problem to retry, not a crash
                    problems = [f"reply has an unexpected shape ({type(e).__name__}: {e})"]
                log.info("%s %s attempt %d: %d problems", base["id"], group["code"], attempt + 1, len(problems))
                if not problems:
                    reply["generator_llm"] = llm.label  # chapters of one base may have different writers
                    seed["groups"][group["code"]] = reply
                    break
            else:
                log.error("%s %s: giving up; last problems: %s", base["id"], group["code"], problems[:5])
                seed.setdefault("failed", {})[group["code"]] = problems
            _save(path, seed)
    return out_dir


def _save(path: Path, seed: dict) -> None:
    path.write_text(json.dumps(seed, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
