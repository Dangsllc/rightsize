"""Explain one scored compliance item: what was asked, how its expected answer was built, and what
the system said. Labels come from construction, so the construction is shown next to the answer.

Everything returned is plain text. System output (hit text, evidence) is untrusted; the core
escapes every string when it renders a view.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from rightsize_core.integrity.dataset import load_suite_config, resolve_document
from rightsize_core.packs.protocol import Highlight, ItemView, UnitRecord, ViewBlock
from rightsize_core.schema import Task

from rightsize_pack_compliance import citations

MUTATIONS = {
    "intact": "left as written: every required element is stated",
    "remove_section": "section deleted",
    "hollow": "heading kept, body replaced with filler",
    "drop_element": "one required element removed",
    "weaken": "one required element reworded so it no longer binds (e.g. 'may' or 'should')",
}


@lru_cache(maxsize=8)
def _corpus(root: Path, suite: str):
    from rightsize_pack_compliance.gen.corpus import Corpus

    cfg = load_suite_config(root, suite)
    return Corpus.load([root / s for s in cfg["sources"]])


@lru_cache(maxsize=8)
def _elements(root: Path, suite: str) -> dict[str, dict]:
    """`<unit code>#<element id>` -> element, from the suite's hand-written elements file."""
    cfg = load_suite_config(root, suite)
    path = (cfg.get("control_classification") or {}).get("elements")
    if not path or not (root / path).exists():
        return {}
    data = yaml.safe_load((root / path).read_text())
    return {f"{u['code']}#{e['id']}": e for g in data["groups"] for u in g["units"] for e in u["elements"]}


def _error(outcome: dict | None) -> dict | None:
    return (outcome or {}).get("error")


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


# --------------------------------------------------------------------------- retrieval


def _match(code: str | None, targets: list[str], hard: set[str], corpus_parts: set[str]) -> tuple[str, str]:
    c = citations.parse(code)
    if c is None:
        return "not a citation", "warn"
    canonical = c.canonical
    if canonical in hard:
        return "hard negative", "bad"
    g = max((citations.grade(c, t) for t in targets), default=0)
    if g == 3:
        return "exact target", "ok"
    if g:
        return "related to the target (partial credit)", "warn"
    if corpus_parts and c.part not in corpus_parts:
        return "outside the corpus", "warn"
    return "unrelated", ""


def retrieval_view(root: Path, task: Task, outcome: dict | None, units: list[UnitRecord]) -> ItemView:
    exp = task.expected
    targets = [r["citation"] for r in exp["relevant"]]
    hard = {citations.normalize(n) for n in exp.get("hard_negatives", [])}
    k = int(task.inputs.get("k", 10))
    hits = (((outcome or {}).get("prediction") or {}).get("hits") or [])[:k]
    rows, marks, first_exact, first_hard = [], [], None, None
    for i, h in enumerate(hits, 1):
        what, mark = _match(h.get("citation"), targets, hard, set(exp.get("corpus_parts", [])))
        if mark == "ok" and first_exact is None:
            first_exact = i
        if what == "hard negative" and first_hard is None:
            first_hard = i
        score = h.get("score")
        rows.append([str(i), h.get("citation") or "—", what, h.get("citation_source", "field"),
                     "—" if score is None else f"{score:.3f}"])
        marks.append(mark)

    err = _error(outcome)
    if err:
        summary, mark = f"No answer: {err['kind']}", "info"
    elif first_exact is None:
        summary, mark = f"Target not in the top {k}", "bad"
    else:
        summary = f"Target found at rank {first_exact}"
        if first_hard is not None and first_hard < first_exact:
            summary += f", after a hard negative at rank {first_hard}"
        mark = "ok" if first_exact == 1 else "warn"

    corpus = _corpus(root, task.suite)
    s = task.strata
    blocks = [ViewBlock(kind="fields", title="Question", fields=[
        ("query", task.inputs.get("query", "")),
        ("style", s.get("query_style", "—")), ("subpart", s.get("subpart", "—")), ("writer", s.get("writer", "—")),
        ("target", ", ".join(targets)),
        ("hard negatives", ", ".join(sorted(h for h in hard if h)) or "none"),
        ("built by", task.provenance.generator), ("seed", task.provenance.seed_artifact or "—"),
    ])]
    if err:
        blocks.append(ViewBlock(kind="notes", title="Error", notes=[f"{err['kind']}: {err['message']}"]))
    for t in targets:
        if t in corpus:
            blocks.append(ViewBlock(kind="document", title=f"Target provision · {t} · {corpus.display_title(t)}",
                                    text=corpus.subtree_text(t)[:6000]))
    blocks.append(ViewBlock(kind="table", title=f"What the system returned (top {k})",
                            columns=["rank", "citation", "match", "source", "score"], rows=rows, row_marks=marks))
    if units and units[0].values:
        v = units[0].values
        blocks.append(ViewBlock(kind="fields", title="Scores for this item", fields=[
            ("ndcg@10", _fmt(v.get("ndcg@10"))), ("recall@5", _fmt(v.get("recall@5"))), ("mrr", _fmt(v.get("mrr"))),
            *([("hard negative ranked first", "yes" if v["confusion@k"] else "no")] if "confusion@k" in v else []),
        ]))
    return ItemView(task_id=task.id, title=task.inputs.get("query", task.id)[:140], summary=summary, mark=mark,
                    blocks=blocks)


# ---------------------------------------------------------------------- classification


def _find(text: str, quote: str) -> tuple[int, int] | None:
    q = " ".join((quote or "").split())
    if len(q) < 12:
        return None
    i = text.find(q)
    if i < 0:
        i = text.lower().find(q.lower())
    return (i, i + len(q)) if i >= 0 else None


def control_view(root: Path, task: Task, outcome: dict | None, units: list[UnitRecord]) -> ItemView:
    cfg = load_suite_config(root, task.suite)
    exp_units: dict[str, dict] = task.expected["units"]
    exp_groups: dict[str, dict] = task.expected.get("groups", {})
    elements = _elements(root, task.suite)
    by_unit = {u.unit_id.split("::", 1)[1]: u for u in units}
    preds = {}
    for a in (((outcome or {}).get("prediction") or {}).get("assessments") or []):
        preds.setdefault(citations.normalize(a.get("control_code")) or a.get("control_code"), a)

    def answer(code: str) -> str:
        u = by_unit.get(code)
        if u is None or u.outcome in ("error", "refusal"):
            return "no answer"
        if u.outcome == "abstained":
            return f"abstained ({u.pred or '—'})"
        if u.outcome == "no_prediction":
            return "not answered"
        return f"{u.pred} (derived from members)" if u.extra.get("derived") else str(u.pred)

    def right(code: str) -> bool:
        u = by_unit.get(code)
        return u is not None and u.outcome == "predicted" and u.pred == u.gold

    group_rows, group_marks = [], []
    for g, e in exp_groups.items():
        group_rows.append([g, e["status"], answer(g), ", ".join(e["members"])])
        group_marks.append("ok" if right(g) else "bad" if g in by_unit and by_unit[g].outcome == "predicted" else "")

    unit_rows, unit_marks, highlights = [], [], []
    try:
        doc = resolve_document(root, cfg, task)
    except OSError:
        doc = task.inputs.get("document", "")
    for code, e in exp_units.items():
        how = MUTATIONS.get(e.get("mutation", "intact"), e.get("mutation", ""))
        dropped = [elements.get(f"{code}#{el}", {}).get("cfr") or el for el in e.get("dropped_elements", [])]
        if dropped and e.get("mutation") in ("drop_element", "weaken"):
            how += ": “" + "”; “".join(_clip(d, 160) for d in dropped) + "”"
        a = preds.get(code) or {}
        conf = a.get("confidence")
        unit_rows.append([code, e.get("group") or "—", e["status"], how, answer(code),
                          "—" if conf is None else f"{conf:.2f}", _clip(a.get("evidence") or "", 240)])
        unit_marks.append("ok" if right(code) else "bad" if code in by_unit and by_unit[code].outcome == "predicted"
                          else "")
        span = _find(doc, a.get("evidence") or "")
        if span:
            highlights.append(Highlight(start=span[0], end=span[1], label=code, mark="ok" if right(code) else "bad"))

    err = _error(outcome)
    g_right = sum(right(g) for g in exp_groups)
    u_right = sum(right(c) for c in exp_units)
    if err:
        summary, mark = f"No answer: {err['kind']}", "info"
    else:
        summary = (f"Requirement groups: {g_right} of {len(exp_groups)} right · "
                   f"requirements: {u_right} of {len(exp_units)} right")
        mark = "ok" if g_right == len(exp_groups) else "bad" if g_right == 0 else "warn"
    s = task.strata
    blocks = [ViewBlock(kind="fields", title="Document", fields=[
        ("document", task.inputs.get("document_id", task.id)),
        *[(k, v) for k, v in s.items()],
        ("built by", task.provenance.generator), ("writer", task.provenance.generator_llm or "—"),
        ("seed", task.provenance.seed_artifact or "—"),
    ])]
    if err:
        blocks.append(ViewBlock(kind="notes", title="Error", notes=[f"{err['kind']}: {err['message']}"]))
    blocks += [
        ViewBlock(kind="table", title="Requirement groups", columns=["group", "expected", "system", "members"],
                  rows=group_rows, row_marks=group_marks),
        ViewBlock(kind="table", title="Requirements: how each expected label was built",
                  columns=["requirement", "group", "expected", "how it was built", "system", "confidence", "evidence"],
                  rows=unit_rows, row_marks=unit_marks),
        ViewBlock(kind="notes", title="Reading the labels", notes=[
            ("Expected labels come from how the document was built, not from anyone's judgment: a deleted or "
             "hollowed section is a gap, and a removed or weakened element makes the requirement partial."),
            "Highlights mark evidence the system quoted, where the quote appears in the document word for word.",
        ]),
    ]
    if doc:
        blocks.append(ViewBlock(kind="document", title="The document as the system saw it", text=doc,
                                highlights=sorted(highlights, key=lambda h: h.start)))
    return ItemView(task_id=task.id, title=task.inputs.get("document_id", task.id), summary=summary, mark=mark,
                    blocks=blocks)
