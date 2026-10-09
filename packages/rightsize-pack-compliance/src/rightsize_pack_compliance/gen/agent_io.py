"""Hand seed-writing and verification work to an agent that is not reachable as an API backend
(e.g. a Claude Code agent), using exactly the prompts `seed_queries` sends to API backends.

    export_write  -> JSONL of {code, style, system, prompt} for this writer's share of targets
    import_write  <- JSONL of {code, style, candidates: [...]}; stored like any other writer's rows
    export_verify -> JSONL of multiple-choice items for rows another writer produced
    import_verify <- JSONL of {code, style, ci, answer: "A".."E"}; stored as this verifier's verdicts

Verification here is lazy: per slot it asks only about the next `per_slot` unasked candidates that
pass the overlap filter (the build keeps the first verified, overlap-clean candidate), records
`null` for the candidates it skipped, which the build treats as unverified, and stops asking once
the slot has an accepted candidate. Re-running export_verify continues where the last round left off.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rightsize_pack_compliance.gen import overlap
from rightsize_pack_compliance.gen.corpus import Corpus
from rightsize_pack_compliance.gen.retrieval import select_targets
from rightsize_pack_compliance.gen.seed_queries import (
    CLAUSE_SYSTEM,
    PROMPT_VERSION,
    SYSTEM,
    VERIFY_SYSTEM,
    SeedStore,
    _negs,
    _prompt,
    mc_item,
    mc_pick,
    neighbors,
)
from rightsize_pack_compliance.gen.text import stable_hash


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def export_write(corpus: Corpus, cfg: dict, store: SeedStore, share: tuple[int, int], out: Path, n: int = 5) -> int:
    rcfg = cfg["retrieval"]
    allow = set(rcfg.get("overlap_filter", {}).get("allow_words", []))
    targets = select_targets(corpus, rcfg)
    by_code = {t.code: t for t in targets}
    negs = _negs(rcfg)
    existing = store.load()
    rows = []
    for t in targets:
        if stable_hash("writer", t.code) % share[1] != share[0]:
            continue
        avoid = neighbors(t, by_code, corpus, negs, distinct=rcfg.get("distinct_neighbors", False))
        for style in (s for s in rcfg["styles"] if s != "exact_term"):
            if (t.code, style) in existing:
                continue
            rows.append({"code": t.code, "style": style, "system": CLAUSE_SYSTEM if style == "clause" else SYSTEM,
                         "prompt": _prompt(t, style, n, allow, avoid)})
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return len(rows)


def import_write(store: SeedStore, label: str, answers: Path) -> int:
    rows = store.load()
    n = 0
    for a in _jsonl(answers):
        cands = [c.strip() for c in a["candidates"] if isinstance(c, str) and c.strip()]
        if cands:
            rows[(a["code"], a["style"])] = {"control_code": a["code"], "style": a["style"], "candidates": cands,
                                             "generator_llm": label, "prompt_version": PROMPT_VERSION}
            n += 1
    store.save(rows)
    return n


def export_verify(corpus: Corpus, cfg: dict, store: SeedStore, label: str, out: Path, per_slot: int = 3) -> int:
    rcfg = cfg["retrieval"]
    ocfg = rcfg.get("overlap_filter", {})
    allow = set(ocfg.get("allow_words", []))
    targets = {t.code: t for t in select_targets(corpus, rcfg)}
    negs = _negs(rcfg)
    items = []
    for key, row in sorted(store.load().items()):
        t = targets.get(row["control_code"])
        if t is None or row.get("generator_llm", "").split(":", 1)[-1] == label.split(":", 1)[-1]:
            continue
        done = row.get("verdicts", {}).get(label, [])
        if any(done):
            continue  # this slot already has a candidate the verifier accepted
        options = [t] + neighbors(t, targets, corpus, negs, distinct=rcfg.get("distinct_neighbors", False))
        asked = 0
        for ci, cand in enumerate(row["candidates"]):
            if asked >= per_slot:
                break
            if ci < len(done) and done[ci] is not None:
                continue  # asked in an earlier round
            if any(re.search(x, cand, re.IGNORECASE) for x in ocfg.get("reject_patterns", [])):
                continue
            if not overlap.check(cand, t.title, t.rule_text, allow, ocfg.get("max_jaccard", 0.25),
                                 ocfg.get("max_shared_ngram", 4)).ok:
                continue
            prompt, _ = mc_item(t, ci, options, cand, row["style"], rcfg)
            items.append({"code": t.code, "style": row["style"], "ci": ci, "system": VERIFY_SYSTEM, "prompt": prompt})
            asked += 1
    out.write_text("".join(json.dumps(r) + "\n" for r in items))
    return len(items)


def import_verify(corpus: Corpus, cfg: dict, store: SeedStore, label: str, answers: Path) -> int:
    rcfg = cfg["retrieval"]
    targets = {t.code: t for t in select_targets(corpus, rcfg)}
    negs = _negs(rcfg)
    rows = store.load()
    n = 0
    for a in _jsonl(answers):
        row = rows[(a["code"], a["style"])]
        t = targets[a["code"]]
        options = [t] + neighbors(t, targets, corpus, negs, distinct=rcfg.get("distinct_neighbors", False))
        _, order = mc_item(t, a["ci"], options, row["candidates"][a["ci"]], row["style"], rcfg)
        pick = mc_pick(str(a.get("answer", "")), order, options)
        res = row.setdefault("verdicts", {}).setdefault(label, [])
        res.extend([None] * (len(row["candidates"]) - len(res)))
        res[a["ci"]] = pick == t.code
        n += 1
    for row in rows.values():  # trim trailing skips so lists stay as short as the work done
        res = row.get("verdicts", {}).get(label)
        while res and res[-1] is None:
            res.pop()
    store.save(rows)
    return n
