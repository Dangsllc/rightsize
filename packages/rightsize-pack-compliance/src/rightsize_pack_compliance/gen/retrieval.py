"""Build retrieval tasks. Labels come from construction: each query is written for one target.

Three styles, in rising difficulty:
* exact_term: a template around the provision's own title
* paraphrase: a practitioner's question, LLM-written once, frozen, and overlap-filtered
* scenario: a short workplace vignette, LLM-written once, frozen, and overlap-filtered
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rightsize_core.schema import Provenance, SourceRef, Task

from rightsize_pack_compliance.gen import overlap
from rightsize_pack_compliance.gen.corpus import Corpus
from rightsize_pack_compliance.gen.splits import assign_splits
from rightsize_pack_compliance.gen.text import stable_hash

log = logging.getLogger(__name__)
GENERATOR = "rightsize_pack_compliance.gen.retrieval@0.1.0"

TEMPLATES = [
    "What does the HIPAA {rule} require for {t}?",
    "HIPAA {rule} requirements for {t}",
    "Which HIPAA {rule} provision covers {t}?",
    "What are the rules on {t} under the HIPAA {rule}?",
]
RULE_BY_SUBPART = {"C": "Security Rule", "D": "Breach Notification Rule", "E": "Privacy Rule"}


@dataclass
class Target:
    code: str
    title: str
    rule_text: str
    subpart: str


def select_targets(corpus: Corpus, cfg: dict) -> list[Target]:
    codes = corpus.security_rule_specs() if cfg.get("include_subpart_c_specs") else []
    codes += [c for c in cfg.get("extra_targets", []) if c not in codes]
    missing = [c for c in codes if c not in corpus]
    if missing:
        raise ValueError(f"Retrieval targets not found in the source corpus: {missing}")
    return [
        Target(c, corpus.display_title(c), corpus.subtree_text(c), corpus.rows[c]["subpart"] or "")
        for c in codes
    ]


def heldout_targets(corpus: Corpus, suite_cfg: dict) -> set[str]:
    targets = select_targets(corpus, suite_cfg["retrieval"])
    splits = assign_splits([t.code for t in targets], suite_cfg["split_seed"], suite_cfg["split_ratios"])
    return {c for c, sp in splits.items() if sp == "heldout"}


def exact_term_query(t: Target) -> str:
    template = TEMPLATES[stable_hash("tmpl", t.code) % len(TEMPLATES)]
    first = t.title.split(" ", 1)[0]
    title = t.title if first.isupper() else t.title[0].lower() + t.title[1:]
    return template.format(t=title, rule=RULE_BY_SUBPART.get(t.subpart, "rules"))


def pick_seed(t: Target, style: str, seeds: dict, ocfg: dict, require_verified: bool = True) -> tuple[str | None, dict]:
    """First candidate that passes the overlap filter and, per candidate, has no verifier
    disagreeing and (when required) at least one verifier agreeing."""
    entry = seeds.get((t.code, style))
    if not entry:
        return None, {"reason": "no seed"}
    allow = set(ocfg.get("allow_words", []))
    rejected = []
    verdicts = entry.get("verdicts", {})
    for ci, cand in enumerate(entry["candidates"]):
        votes = {v: res[ci] for v, res in verdicts.items() if ci < len(res)}
        if any(ok is False for ok in votes.values()):
            rejected.append({"candidate": cand, "reason": f"verifier disagreed: {sorted(v for v, ok in votes.items() if not ok)}"})
            continue
        if require_verified and not any(votes.values()):
            rejected.append({"candidate": cand, "reason": "not verified"})
            continue
        chk = overlap.check(cand, t.title, t.rule_text, allow, ocfg.get("max_jaccard", 0.25),
                            ocfg.get("max_shared_ngram", 4))
        if chk.ok:
            return cand, {"generator_llm": entry.get("generator_llm"), "rejected": rejected,
                          "jaccard": round(chk.jaccard, 3), "verified_by": sorted(v for v, ok in votes.items() if ok)}
        rejected.append({"candidate": cand, "reason": chk.reason})
    return None, {"reason": "all candidates rejected", "rejected": rejected}


def build(corpus: Corpus, suite_cfg: dict, seeds: dict, seed_artifact: str | None,
          include_heldout: bool = True) -> tuple[list[Task], list[dict]]:
    rcfg = suite_cfg["retrieval"]
    require_verified = bool(rcfg.get("require_verified", True))
    targets = select_targets(corpus, rcfg)
    splits = assign_splits([t.code for t in targets], suite_cfg["split_seed"], suite_cfg["split_ratios"])
    negs: dict[str, set[str]] = {}
    for a, b in rcfg.get("hard_negative_pairs", []):
        for x, y in ((a, b), (b, a)):
            if x not in corpus or y not in corpus:
                raise ValueError(f"Hard-negative pair references an unknown citation: {a}, {b}")
            negs.setdefault(x, set()).add(y)

    tasks: list[Task] = []
    report: list[dict] = []
    for t in targets:
        if splits[t.code] == "heldout" and not include_heldout:
            continue
        for style in rcfg["styles"]:
            if style == "exact_term" and splits[t.code] == "heldout":
                continue  # a template over a public title can be rebuilt by anyone: not held out
            meta: dict = {}
            if style == "exact_term":
                query = exact_term_query(t)
                llm = None
            else:
                query, meta = pick_seed(t, style, seeds, rcfg.get("overlap_filter", {}), require_verified)
                llm = meta.get("generator_llm")
            report.append({"target": t.code, "style": style, "ok": query is not None, **meta})
            if query is None:
                continue
            tasks.append(Task(
                id=f"{suite_cfg['suite']}.ret.{t.code}.{style}",
                suite=suite_cfg["suite"],
                pack="compliance",
                task_type="retrieval",
                split=splits[t.code],
                strata={"query_style": style, "subpart": t.subpart,
                        "writer": (llm or "template").split(":", 1)[-1]},
                inputs={"query": query, "k": rcfg.get("k", 10)},
                expected={
                    "relevant": [{"citation": t.code, "grade": 3}],
                    "hard_negatives": sorted(negs.get(t.code, [])),
                    "corpus_parts": rcfg.get("corpus_parts", []),
                    "cluster": t.code,
                },
                metric={"primary": "ndcg@10", "secondary": ["recall@5", "mrr", "confusion@k"]},
                provenance=Provenance(
                    generator=GENERATOR,
                    seed=suite_cfg["split_seed"],
                    seed_artifact=seed_artifact if style != "exact_term" else None,
                    source=SourceRef(citation=f"45 CFR {t.code}", edition=suite_cfg["edition"]),
                    generator_llm=llm,
                ),
                canary=suite_cfg["canary"],
            ))
    seen: dict[str, str] = {}
    for task in tasks:
        q = task.inputs["query"].strip().lower()
        if q in seen:
            raise ValueError(f"Duplicate query text for {seen[q]} and {task.id}: {task.inputs['query']!r}")
        seen[q] = task.id
    return tasks, report
