"""Build labelled policy manuals from the frozen base manuals. Fully deterministic.

For each base manual and each variant index, every requirement group draws a label from a
cycling pattern (so labels balance at the *group* level, where group-level systems answer), and
member units get mutations consistent with it:

    covered  every member intact
    gap      every member removed or hollowed (heading kept, body replaced with filler)
    partial  one member weakened or has one element dropped (if it has 2+ elements),
             otherwise one member removed; the rest intact

Spec-level labels follow from what was done to each unit. A leakage audit then checks that no
removed or weakened element is still met somewhere else in the same manual; affected units are
dropped from scoring and reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from rightsize_core.schema import Provenance, SourceRef, Task

from rightsize_pack_compliance.gen.corpus import Corpus
from rightsize_pack_compliance.gen.seed_manuals import base_seed_path, load_elements, matches
from rightsize_pack_compliance.gen.text import sha256_text, stable_hash

GENERATOR = "rightsize_pack_compliance.gen.manuals@0.1.0"

FILLER = [
    "This section is under review and will be completed in a future revision of this manual.",
    "Department managers should contact the compliance office for current guidance on this topic.",
    "Content for this section is being consolidated from prior policy versions.",
    "Refer to the policy owner for information about how this area is handled today.",
]


@dataclass
class UnitPlan:
    code: str
    group: str
    mutation: str = "intact"  # intact | remove_section | hollow | drop_element | weaken
    element: str | None = None  # element affected by drop_element / weaken
    status: str = "covered"


@dataclass
class ManualPlan:
    manual_id: str
    base: str
    index: int
    groups: dict[str, str] = field(default_factory=dict)  # group code -> label
    units: dict[str, UnitPlan] = field(default_factory=dict)


def plan_manual(base_id: str, base_index: int, index: int, groups: list[dict], pattern: list[str]) -> ManualPlan:
    mp = ManualPlan(manual_id=f"{base_id}-{index:02d}", base=base_id, index=index)
    L = len(pattern)
    for gi, g in enumerate(groups):
        label = pattern[(gi * 3 + index + base_index * 5) % L]
        units = g["units"]
        if label == "partial" and len(units) == 1 and len(units[0]["elements"]) < 2:
            # A one-element, one-unit group cannot be partially met. Take the next label.
            label = "covered" if (gi + index) % 2 == 0 else "gap"
        mp.groups[g["code"]] = label
        def h(*k, _g=g["code"]):
            return stable_hash(base_id, index, _g, *k)

        for u in units:
            mp.units[u["code"]] = UnitPlan(code=u["code"], group=g["code"])
        if label == "gap":
            for u in units:
                p = mp.units[u["code"]]
                p.mutation = "hollow" if h(u["code"], "gap") % 2 else "remove_section"
                p.status = "gap"
        elif label == "partial":
            # Prefer a member that can be partially met (2+ elements), so spec-level "partial"
            # labels exist too; otherwise one member goes missing and the group is partial.
            multi = [u for u in units if len(u["elements"]) >= 2]
            pool = multi if multi and h("pool") % 4 else units
            target = pool[h("pick") % len(pool)]
            # About half of the other multi-element members are partly met as well; the group stays
            # partial either way (some but not all of it is met).
            chosen = [target] + [u for u in multi if u is not target and h("also", u["code"]) % 2 == 0]
            for u in chosen:
                p = mp.units[u["code"]]
                els = u["elements"]
                if len(els) >= 2:
                    # Never remove an element that a remaining element presupposes (implied_by):
                    # the unit would still read as met, and "partial" would be the wrong label.
                    droppable = [e for e in els if not e.get("implied_by")]
                    p.mutation = "weaken" if h("mut", u["code"]) % 2 else "drop_element"
                    p.element = droppable[h("el", u["code"]) % len(droppable)]["id"]
                    p.status = "partial"
                else:
                    # Weakening a unit's only element would leave vague language behind, which most
                    # assessors call "partial"; removing or hollowing it is an unambiguous gap.
                    p.mutation = "hollow" if h("mut", u["code"]) % 2 else "remove_section"
                    p.element = None
                    p.status = "gap"
    return mp


def _unit_body(section: dict, plan: UnitPlan) -> list[str]:
    out = []
    for s in section["sentences"]:
        el = s.get("element")
        if plan.mutation == "drop_element" and el == plan.element:
            continue
        if plan.mutation == "weaken" and el == plan.element:
            out.append(section["weakened"][el])
            continue
        out.append(s["text"])
    return out


def render_manual(seed: dict, elements: dict, mp: ManualPlan, canary: str) -> tuple[str, dict[str, str]]:
    """Return the manual text and, for the audit, the text of each rendered unit section."""
    front = seed["front"]
    lines = [f"<!-- {canary} -->", f"# {front['title']}", ""]
    for para in front["preamble"]:
        lines += [para, ""]
    distractors = list(front["distractors"])
    near = list(front["near_misses"])
    h = lambda *k: stable_hash(mp.manual_id, *k)
    distractors.sort(key=lambda d: h("d", d["heading"]))
    near.sort(key=lambda d: h("n", d["heading"]))
    extras = distractors[:4] + near[:2]
    n_groups = len(elements["groups"])
    slots = {h("slot", i) % (n_groups + 1): x for i, x in enumerate(extras)}
    insert_at: dict[int, list[dict]] = {}
    for i, x in enumerate(extras):
        insert_at.setdefault(h("slot", i) % (n_groups + 1), []).append(x)
    del slots

    unit_text: dict[str, str] = {}
    chapter = 0

    def emit_extra(pos: int) -> None:
        nonlocal chapter
        for x in insert_at.get(pos, []):
            chapter += 1
            lines.extend([f"## {chapter}. {x['heading']}", "", x["body"], ""])

    for gi, g in enumerate(elements["groups"]):
        emit_extra(gi)
        gseed = seed["groups"][g["code"]]
        secs = {s["unit"]: s for s in gseed["sections"]}
        visible = [u for u in g["units"] if mp.units[u["code"]].mutation != "remove_section"]
        if not visible:
            continue
        chapter += 1
        lines += [f"## {chapter}. {gseed['heading']}", "", gseed["intro"], ""]
        for sub, u in enumerate(visible, start=1):
            plan = mp.units[u["code"]]
            sec = secs[u["code"]]
            lines.append(f"### {chapter}.{sub} {sec['heading']}")
            lines.append("")
            if plan.mutation == "hollow":
                body = [FILLER[h("filler", u["code"]) % len(FILLER)]]
            else:
                body = _unit_body(sec, plan)
            text = " ".join(body)
            unit_text[u["code"]] = text
            lines += [text, ""]
    emit_extra(n_groups)
    return "\n".join(lines).rstrip() + "\n", unit_text


def audit(elements: dict, mp: ManualPlan, unit_text: dict[str, str]) -> list[dict]:
    """Every element a mutation removed must not still be met in another unit's section."""
    leaks = []
    for g in elements["groups"]:
        for u in g["units"]:
            plan = mp.units[u["code"]]
            if plan.mutation == "intact":
                continue
            removed = [e for e in u["elements"]
                       if plan.mutation in ("remove_section", "hollow") or e["id"] == plan.element]
            for e in removed:
                for other, text in unit_text.items():
                    if other == u["code"]:
                        if plan.mutation in ("weaken", "drop_element", "hollow") and matches(text, e):
                            leaks.append({"unit": u["code"], "element": e["id"], "found_in": other})
                        continue
                    if matches(text, e):
                        leaks.append({"unit": u["code"], "element": e["id"], "found_in": other})
    return leaks


def build(root: Path, corpus: Corpus, cfg: dict) -> tuple[list[Task], dict[str, str], dict, list[Path]]:
    ccfg = cfg["control_classification"]
    elements = load_elements(root / ccfg["elements"])
    tasks: list[Task] = []
    docs: dict[str, str] = {}
    report: dict = {"manuals": [], "leaks": [], "missing_bases": []}
    inputs: list[Path] = [root / ccfg["elements"]]
    groups_meta = {g["code"]: [u["code"] for u in g["units"]] for g in elements["groups"]}

    for bi, base in enumerate(ccfg["bases"]):
        path = base_seed_path(root, cfg, base)
        if not path.exists():
            if not base.get("heldout"):  # held-out bases are simply absent from public clones
                report["missing_bases"].append(base["id"])
            continue
        seed = json.loads(path.read_text())
        missing_groups = [g["code"] for g in elements["groups"] if g["code"] not in seed.get("groups", {})]
        if missing_groups or "front" not in seed:
            if not base.get("heldout"):  # the public report says nothing about held-out bases
                report["missing_bases"].append(f"{base['id']} (incomplete: {missing_groups or 'front matter'})")
            continue
        if not base.get("heldout"):
            inputs.append(path)
        n = ccfg["manuals_per_base"]
        ids = [f"{base['id']}-{i:02d}" for i in range(n)]
        order = sorted(ids, key=lambda m: stable_hash(cfg["split_seed"], m))
        sp = ccfg["split_per_base"]
        if base.get("heldout"):
            split_of = {m: "heldout" for m in ids}
        else:
            split_of = {m: ("public" if i < sp["public"] else "dev") for i, m in enumerate(order)}
        for i in range(n):
            mp = plan_manual(base["id"], bi, i, elements["groups"], ccfg["label_pattern"])
            text, unit_text = render_manual(seed, elements, mp, cfg["canary"])
            leaks = audit(elements, mp, unit_text)
            leaked_units = {lk["unit"] for lk in leaks}
            report["leaks"] += [{"manual": mp.manual_id, **lk} for lk in leaks]
            split = split_of[mp.manual_id]
            doc_rel = f"{split}/{mp.manual_id}.md"
            docs[doc_rel] = text
            exp_units, unit_inputs = {}, []
            for g in elements["groups"]:
                for u in g["units"]:
                    plan = mp.units[u["code"]]
                    r = corpus.rows[u["code"]]
                    unit_inputs.append({
                        "control_code": u["code"], "title": corpus.display_title(u["code"]),
                        "specification": r["text"], "group": g["code"], "standard_type": r["standard_type"],
                    })
                    if u["code"] in leaked_units:
                        continue
                    writer = seed["groups"][g["code"]].get("generator_llm") or seed.get("generator_llm") or base["writer"]
                    exp_units[u["code"]] = {
                        "status": plan.status, "mutation": plan.mutation, "group": g["code"],
                        "writer": writer.split(":", 1)[-1],
                        "dropped_elements": [plan.element] if plan.element else (
                            [e["id"] for e in u["elements"]] if plan.status == "gap" else []),
                        "standard_type": r["standard_type"],
                    }
            exp_groups = {
                gc: {"status": label, "members": groups_meta[gc]}
                for gc, label in mp.groups.items()
                if not (set(groups_meta[gc]) & leaked_units)
            }
            tasks.append(Task(
                id=f"{cfg['suite']}.cls.{mp.manual_id}",
                suite=cfg["suite"], pack="compliance", task_type="control_classification", split=split,
                strata={"base": base["id"]},
                inputs={
                    "document_id": mp.manual_id, "document_ref": f"{mp.manual_id}.md",
                    "document_sha256": sha256_text(text), "units": unit_inputs, "groups": groups_meta,
                },
                expected={"units": exp_units, "groups": exp_groups},
                metric={"primary": "group.macro_f1", "secondary": ["spec.macro_f1", "spec.deficiency_recall"]},
                provenance=Provenance(
                    generator=GENERATOR, seed=cfg["split_seed"], seed_artifact=str(path.relative_to(root)),
                    source=SourceRef(citation="45 CFR 164 Subpart C", edition=cfg["edition"]),
                    generator_llm=seed.get("generator_llm"),
                ),
                canary=cfg["canary"],
            ))
            report["manuals"].append({
                "manual": mp.manual_id, "split": split, "groups": mp.groups,
                "leaked_units": sorted(leaked_units),
            })

    # label balance at both levels
    from collections import Counter

    pub = [t for t in tasks if t.split != "heldout"]
    gl = Counter(g["status"] for t in pub for g in t.expected["groups"].values())
    sl = Counter(u["status"] for t in pub for u in t.expected["units"].values())
    report["label_mix_public"] = {"group": dict(sorted(gl.items())), "spec": dict(sorted(sl.items()))}
    return tasks, docs, report, inputs
