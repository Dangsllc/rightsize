"""Write the frozen paraphrase / scenario seeds for retrieval. Run once; commit the output.

Each target gets several candidates per style; the deterministic build keeps the first one that
passes the overlap filter. Re-running this changes the dataset, so it bumps the seed version.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rightsize_pack_compliance import citations
from rightsize_pack_compliance.gen.corpus import Corpus
from rightsize_pack_compliance.gen.llm import SeedLLM, items_of, parse_json_block
from rightsize_pack_compliance.gen.retrieval import Target, select_targets
from rightsize_pack_compliance.gen.text import content_words, stable_hash

log = logging.getLogger(__name__)
PROMPT_VERSION = "retrieval-seed-v1"

SYSTEM = (
    "You write realistic questions that people at hospitals, clinics, and health-tech companies "
    "ask about HIPAA. You never quote regulations and never cite section numbers."
)

STYLE_INSTRUCTIONS = {
    "paraphrase": (
        "Write {n} different questions a compliance officer, practice manager, or IT lead might "
        "ask whose answer is exactly the provision below. Each is one sentence, phrased in plain "
        "workplace language."
    ),
    "scenario": (
        "Write {n} different short workplace situations (2 to 3 sentences each) that end with a "
        "question, where the provision below is the rule that answers the question. Describe what "
        "happened; do not name the rule."
    ),
}


def _prompt(t: Target, style: str, n: int, allow: set[str], avoid: list[Target] | None = None) -> str:
    banned = sorted(set(content_words(t.title, allow)))
    avoid_txt = ""
    if avoid:
        avoid_txt = "\n\nNearby provisions that must NOT be the answer (write questions only the target answers):\n" + "\n".join(
            f"- {a.title}: {a.rule_text[:300]}" for a in avoid
        )
    return (
        STYLE_INSTRUCTIONS[style].format(n=n)
        + " The question must be specifically answered by this provision, not a general HIPAA question."
        + "\n\nRules:\n- Do not include section numbers or the word 'CFR'.\n"
        + "- Do not copy phrases from the provision text.\n"
        + (f"- Do not use any of these words: {', '.join(banned)}.\n" if banned else "")
        + '- Return only a JSON object of the form {"items": ["...", "..."]}.\n\n'
        + f"Provision title: {t.title}\nProvision text: {t.rule_text[:2500]}"
        + avoid_txt
    )


def neighbors(t: Target, targets: dict[str, Target], corpus: Corpus, negs: dict[str, set[str]], n: int = 4) -> list[Target]:
    """Confusable provisions for a target: curated hard negatives first, then siblings and
    cousins in the same section, chosen deterministically."""
    out: list[str] = sorted(negs.get(t.code, set()))
    section = corpus.rows[t.code]["section"]
    same = sorted(
        (c for c, r in corpus.rows.items()
         if r["section"] == section and c != t.code and r.get("title") and len(r.get("text", "")) > 40
         and not citations.parse(c).is_ancestor_of(citations.parse(t.code))
         and not citations.parse(t.code).is_ancestor_of(citations.parse(c))),
        key=lambda c: stable_hash("nb", t.code, c),
    )
    for c in same:
        if len(out) >= n:
            break
        if c not in out:
            out.append(c)
    res = []
    for c in out[:n]:
        res.append(targets.get(c) or Target(c, corpus.display_title(c), corpus.subtree_text(c), corpus.rows[c]["subpart"] or ""))
    return res


def _negs(rcfg: dict) -> dict[str, set[str]]:
    negs: dict[str, set[str]] = {}
    for a, b in rcfg.get("hard_negative_pairs", []):
        negs.setdefault(a, set()).add(b)
        negs.setdefault(b, set()).add(a)
    return negs


def generate(corpus: Corpus, suite_cfg: dict, llm: SeedLLM, store: SeedStore, n: int = 5,
             styles: tuple[str, ...] = ("paraphrase", "scenario"), share: tuple[int, int] = (0, 1),
             retry: set[tuple[str, str]] | None = None) -> Path:
    """Write seeds for this writer's share of targets: those with hash % share[1] == share[0]."""
    rcfg = suite_cfg["retrieval"]
    allow = set(rcfg.get("overlap_filter", {}).get("allow_words", []))
    existing = store.load()
    retry = set(retry or ())
    all_targets = select_targets(corpus, rcfg)
    by_code = {t.code: t for t in all_targets}
    negs = _negs(rcfg)
    targets = [t for t in all_targets if stable_hash("writer", t.code) % share[1] == share[0]]
    for i, t in enumerate(targets):
        avoid = neighbors(t, by_code, corpus, negs)
        for style in styles:
            if (t.code, style) in existing and (t.code, style) not in retry:
                continue
            for attempt in range(3):
                try:
                    reply = llm.complete(SYSTEM, _prompt(t, style, n, allow, avoid), json_mode=True)
                    cands = [c.strip() for c in items_of(parse_json_block(reply)) if isinstance(c, str) and c.strip()]
                    if cands:
                        break
                except Exception as e:  # noqa: BLE001 - retry any backend hiccup
                    log.warning("seed %s/%s attempt %d failed: %s", t.code, style, attempt + 1, e)
            else:
                log.error("giving up on %s/%s", t.code, style)
                continue
            if (t.code, style) in retry:
                # Append fresh candidates; earlier ones (and their verdicts, by index) stay put.
                existing[(t.code, style)]["candidates"] += cands
                retry.discard((t.code, style))
            else:
                existing[(t.code, style)] = {
                    "control_code": t.code, "style": style, "candidates": cands,
                    "generator_llm": llm.label, "prompt_version": PROMPT_VERSION,
                }
            log.info("[%d/%d] %s %s: %d candidates", i + 1, len(targets), t.code, style, len(cands))
            store.save(existing)
    store.save(existing)
    return store.public


VERIFY_SYSTEM = "You are an expert in the HIPAA Privacy, Security, and Breach Notification Rules."


def verify(corpus: Corpus, suite_cfg: dict, llm: SeedLLM, store: SeedStore) -> Path:
    """Multiple-choice check: a model other than the writer must pick the target provision out
    of a lineup of its confusable neighbors. Verdicts are stored next to each candidate; the
    build keeps only candidates the verifier attributed to the target."""
    rcfg = suite_cfg["retrieval"]
    rows = store.load()
    targets = {t.code: t for t in select_targets(corpus, rcfg)}
    negs = _negs(rcfg)
    letters = "ABCDE"
    for key in sorted(rows):
        row = rows[key]
        t = targets.get(row["control_code"])
        if t is None:
            continue
        if row.get("generator_llm", "").split(":", 1)[-1] == llm.label.split(":", 1)[-1]:
            continue  # a model never verifies its own questions
        verdicts = row.setdefault("verdicts", {})
        if llm.label in verdicts and len(verdicts[llm.label]) == len(row["candidates"]):
            continue
        options = [t] + neighbors(t, targets, corpus, negs)
        res = list(verdicts.get(llm.label, []))
        for ci, cand in enumerate(row["candidates"]):
            if ci < len(res):
                continue
            order = sorted(range(len(options)), key=lambda i: stable_hash("mc", t.code, ci, i))
            lines = [f"{letters[j]}. {options[i].title}: {options[i].rule_text[:500]}" for j, i in enumerate(order)]
            user = (
                f"Question: {cand}\n\nWhich provision most specifically answers this question?\n\n"
                + "\n".join(lines)
                + '\n\nReply with only a JSON object like {"answer": "A"}.'
            )
            pick = None
            for _ in range(2):
                try:
                    ans = parse_json_block(llm.complete(VERIFY_SYSTEM, user, max_tokens=200, json_mode=True))
                    letter = str(ans.get("answer", "")).strip().upper()[:1] if isinstance(ans, dict) else ""
                    if letter in letters[: len(options)]:
                        pick = options[order[letters.index(letter)]].code
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("verify %s #%d failed: %s", t.code, ci, e)
            res.append(pick == t.code)
        verdicts[llm.label] = res
        log.info("verified %s %s: %d/%d", t.code, row["style"], sum(res), len(res))
        store.save(rows)
    store.save(rows)
    return store.public


class SeedStore:
    """Seed rows for public/dev targets live in data/; rows for held-out targets live in the
    private held-out directory, so a public clone cannot rebuild the held-out split."""

    def __init__(self, public: Path, private: Path | None, heldout: set[str], canary: str | None) -> None:
        self.public, self.private, self.heldout, self.canary = public, private, heldout, canary

    def load(self) -> dict:
        rows = {}
        for path in (self.public, self.private):
            if path and path.exists():
                for line in path.read_text().splitlines():
                    if line.strip():
                        r = json.loads(line)
                        rows[(r["control_code"], r["style"])] = r
        return rows

    def save(self, rows: dict) -> None:
        pub = {k: v for k, v in rows.items() if k[0] not in self.heldout}
        priv = {k: v for k, v in rows.items() if k[0] in self.heldout}
        _write(self.public, pub, self.canary)
        if self.private is not None and (priv or self.private.exists()):
            _write(self.private, priv, self.canary)
        elif priv:
            raise RuntimeError("held-out seed rows but no private directory configured")


def store_for(root: Path, suite_cfg: dict, corpus: Corpus, name: str = "retrieval_queries.v1.jsonl") -> SeedStore:
    from rightsize_core.integrity.dataset import heldout_dir

    from rightsize_pack_compliance.gen.retrieval import heldout_targets

    return SeedStore(root / "data" / "seeds" / name, heldout_dir(root, suite_cfg).parent / "seeds" / name,
                     heldout_targets(corpus, suite_cfg), suite_cfg.get("canary"))


def _write(out: Path, rows: dict, canary: str | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for key in sorted(rows):
            row = dict(rows[key])
            if canary:
                row["canary"] = canary
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load(path: Path) -> dict:
    """Single-file reader (kept for scripts); prefer SeedStore."""
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {(r["control_code"], r["style"]): r for r in rows}
