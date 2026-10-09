"""Build labelled business associate agreements from the frozen base agreements. Deterministic.

Labels are planned exactly as for policy manuals (`manuals.plan_manual`): every requirement group
draws covered / partial / gap from a cycling pattern and its member units are mutated to match.
Agreements have no "hollowed" sections, so a gap is always a removal: the unit's sentences are
simply absent. Each variant is rendered in both presentations of its base (`template` and
`vendor_paper`) with the same plan, so the two documents carry identical labels; they share a
bootstrap cluster (`expected.cluster`), which keeps the repeated measure from inflating n.

A leakage audit checks that no removed or weakened element is still met anywhere in the rendered
document; affected units are dropped from scoring and reported. The seed checker makes this a
safety net rather than an expected outcome.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from rightsize_core.schema import Provenance, SourceRef, Task

from rightsize_pack_compliance.gen.corpus import Corpus
from rightsize_pack_compliance.gen.manuals import ManualPlan, plan_manual
from rightsize_pack_compliance.gen.seed_baas import (
    PRESENTATIONS,
    base_seed_path,
    check_seed,
    load_elements,
    meets,
    sentences,
    tags_of,
)
from rightsize_pack_compliance.gen.text import sha256_text, stable_hash

GENERATOR = "rightsize_pack_compliance.gen.baas@0.1.0"
SOURCE_CITATION = "45 CFR 164.504(e)(2), 164.314(a)(2)(i), 164.410"


def _keep(tag: str | None, mp: ManualPlan) -> str:
    """'keep', 'drop', or 'weaken' for a sentence with this tag under the plan."""
    if not tag:
        return "keep"
    unit, _, el = tag.partition("#")
    plan = mp.units[unit]
    if plan.mutation == "remove_section":
        return "drop"  # the unit's elements and its supporting (#ctx) language
    if el == "ctx":
        return "keep"
    if plan.element == el:
        return "drop" if plan.mutation == "drop_element" else "weaken"
    return "keep"


def render(pres: dict, mp: ManualPlan, canary: str) -> tuple[str, list[str]]:
    """Return the agreement text and every rendered sentence (for the audit)."""
    weakened = pres["weakened"]
    blocks = []
    for b in pres["blocks"]:
        out = []
        for s in b.get("sentences", []):
            action = _keep(s.get("tag"), mp)
            if action == "drop":
                continue
            out.append(weakened[s["tag"]] if action == "weaken" else s["text"])
        blocks.append((b, out))

    lines = [f"<!-- {canary} -->", f"# {pres['title']}", ""]
    rendered: list[str] = []
    article = 0
    letter = 0
    for i, (b, text) in enumerate(blocks):
        kind = b["kind"]
        if kind == "article":
            # An article whose body and clauses all went away leaves no heading behind.
            following = []
            for nb, nt in blocks[i + 1:]:
                if nb["kind"] == "article":
                    break
                following.append(nt)
            if not text and not any(following):
                continue
            article += 1
            letter = 0
            num = b.get("number") or str(article)
            lines += [f"## {num}. {b['heading']}" if b.get("heading") else f"## {num}.", ""]
            if text:
                lines += [" ".join(text), ""]
        elif not text:
            continue
        elif kind == "clause":
            body = " ".join(text)
            head = f"**{b['heading']}.** " if b.get("heading") else ""
            if b.get("number"):
                lines += [f"**{b['number']}** {head}{body}".replace("** **", " "), ""]
            else:
                lines += [f"({chr(ord('a') + letter)}) {head}{body}", ""]
                letter += 1
        else:
            head = f"**{b['heading']}** " if b.get("heading") else ""
            lines += [f"{head}{' '.join(text)}", ""]
        rendered += [x for t in text for x in sentences(t)]
    return "\n".join(lines).rstrip() + "\n", rendered


def audit(elements: dict, mp: ManualPlan, rendered: list[str]) -> list[dict]:
    """No element a mutation removed or weakened may still be met anywhere in the document."""
    leaks = []
    for tag, e in tags_of(elements).items():
        unit, _, el = tag.partition("#")
        plan = mp.units[unit]
        if plan.mutation == "intact" or (plan.mutation != "remove_section" and plan.element != el):
            continue
        if any(meets(s, e) for s in rendered):
            leaks.append({"unit": unit, "element": el})
    return leaks


def _doc_id(pair: str, pres: str) -> str:
    """Opaque per-document id: which of a/b is which presentation varies by pair."""
    flip = stable_hash("pres", pair) % 2
    return f"{pair}{'ab'[(PRESENTATIONS.index(pres) + flip) % 2]}"


def build(root: Path, corpus: Corpus, cfg: dict) -> tuple[list[Task], dict[str, str], dict, list[Path]]:
    ccfg = cfg["control_classification"]
    elements = load_elements(root / ccfg["elements"])
    units = {u["code"]: (g, u) for g in elements["groups"] for u in g["units"]}
    groups_meta = {g["code"]: [u["code"] for u in g["units"]] for g in elements["groups"]}
    tasks: list[Task] = []
    docs: dict[str, str] = {}
    report: dict = {"agreements": [], "leaks": [], "missing_bases": []}
    inputs: list[Path] = [root / ccfg["elements"]]

    for bi, base in enumerate(ccfg["bases"]):
        path = base_seed_path(root, cfg, base)
        if not path.exists():
            if not base.get("heldout"):
                report["missing_bases"].append(base["id"])
            continue
        seed = json.loads(path.read_text())
        problems = check_seed(seed, elements)
        if problems:
            if not base.get("heldout"):
                report["missing_bases"].append(f"{base['id']} ({len(problems)} seed problems)")
            continue
        if not base.get("heldout"):
            inputs.append(path)
        n = ccfg["variants_per_base"]
        pairs = [f"{base['id']}-{i:02d}" for i in range(n)]
        order = sorted(pairs, key=lambda p: stable_hash(cfg["split_seed"], p))
        sp = ccfg["split_per_base"]
        split_of = ({p: "heldout" for p in pairs} if base.get("heldout") else
                    {p: ("public" if i < sp["public"] else "dev") for i, p in enumerate(order)})
        writer = seed["generator_llm"].split(":", 1)[-1]
        for i, pair in enumerate(pairs):
            mp = plan_manual(base["id"], bi, i, elements["groups"], ccfg["label_pattern"], allow_hollow=False)
            split = split_of[pair]
            for pres in PRESENTATIONS:
                text, rendered = render(seed["presentations"][pres], mp, cfg["canary"])
                leaks = audit(elements, mp, rendered)
                leaked = {lk["unit"] for lk in leaks}
                doc_id = _doc_id(pair, pres)
                report["leaks"] += [{"agreement": doc_id, "split": split, **lk} for lk in leaks]
                docs[f"{split}/{doc_id}.md"] = text
                unit_inputs, exp_units = [], {}
                for code, (g, u) in units.items():
                    r = corpus.rows[code]
                    unit_inputs.append({"control_code": code, "title": u["title"], "specification": r["text"],
                                        "group": g["code"], "standard_type": r["standard_type"]})
                    if code in leaked:
                        continue
                    plan = mp.units[code]
                    exp_units[code] = {
                        "status": plan.status, "mutation": plan.mutation, "group": g["code"], "writer": writer,
                        "dropped_elements": [plan.element] if plan.element else (
                            [e["id"] for e in u["elements"]] if plan.status == "gap" else []),
                        "standard_type": r["standard_type"],
                    }
                exp_groups = {gc: {"status": label, "members": groups_meta[gc]}
                              for gc, label in mp.groups.items() if not (set(groups_meta[gc]) & leaked)}
                tasks.append(Task(
                    id=f"{cfg['suite']}.cls.{pair}.{pres}",
                    suite=cfg["suite"], pack="compliance", task_type="control_classification", split=split,
                    strata={"base": base["id"], "doc_family": "baa", "presentation": pres,
                            "derivation": seed["derivation"] if pres == "template" else "paraphrased"},
                    inputs={"document_id": doc_id, "document_ref": f"{doc_id}.md",
                            "document_sha256": sha256_text(text), "units": unit_inputs, "groups": groups_meta},
                    expected={"units": exp_units, "groups": exp_groups, "cluster": pair},
                    metric={"primary": "group.macro_f1", "secondary": ["spec.macro_f1", "spec.deficiency_recall"]},
                    provenance=Provenance(
                        generator=GENERATOR, seed=cfg["split_seed"],
                        seed_artifact=None if base.get("heldout") else str(path.relative_to(root)),
                        source=SourceRef(citation=SOURCE_CITATION, edition=cfg["edition"]),
                        generator_llm=seed["generator_llm"],
                    ),
                    canary=cfg["canary"],
                ))
                report["agreements"].append({"agreement": doc_id, "pair": pair, "presentation": pres,
                                             "split": split, "groups": mp.groups, "words": len(text.split()),
                                             "leaked_units": sorted(leaked)})

    pub = [t for t in tasks if t.split != "heldout"]
    gl = Counter(g["status"] for t in pub for g in t.expected["groups"].values())
    sl = Counter(u["status"] for t in pub for u in t.expected["units"].values())
    report["label_mix_public"] = {"group": dict(sorted(gl.items())), "spec": dict(sorted(sl.items()))}
    return tasks, docs, report, inputs
