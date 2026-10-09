from pathlib import Path

import pytest
from rightsize_pack_compliance.gen import seed_baas
from rightsize_pack_compliance.gen.baas import _doc_id, audit, render
from rightsize_pack_compliance.gen.manuals import plan_manual
from rightsize_pack_compliance.gen.seed_baas import check_seed, load_elements, meets, tags_of

ROOT = Path(__file__).resolve().parents[2]

ELEMENTS = {
    "version": 1,
    "groups": [
        {"code": "baa.g1", "topic": "t1", "units": [
            {"code": "U1", "title": "u1", "elements": [
                {"id": "E1", "signature": ["alpha"]},
                {"id": "E2", "signature": ["beta"]},
            ]},
            {"code": "U2", "title": "u2", "elements": [{"id": "E1", "signature": ["gamma"]}]},
        ]},
        {"code": "baa.g2", "topic": "t2", "units": [
            {"code": "U3", "title": "u3", "elements": [{"id": "E1", "signature": ["delta"]}]},
        ]},
        {"code": "baa.g3", "topic": "t3", "units": [
            {"code": "U4", "title": "u4", "elements": [
                {"id": "E1", "signature": ["epsilon"]},
                {"id": "E2", "signature": ["zeta"]},
            ]},
        ]},
    ],
}
WORDS = {"alpha": "U1#E1", "beta": "U1#E2", "gamma": "U2#E1", "delta": "U3#E1", "epsilon": "U4#E1", "zeta": "U4#E2"}


def _presentation(numbered: bool) -> dict:
    blocks = [{"kind": "para", "heading": None, "sentences": [{"text": "This Agreement is between the parties.", "tag": None}]}]
    for i, (word, tag) in enumerate(WORDS.items()):
        blocks.append({"kind": "article" if i % 3 == 0 else "clause", "heading": f"Heading {word}",
                       "number": f"{i + 1}.1" if numbered else None,
                       "sentences": [{"text": f"Vendor shall do {word} work.", "tag": tag},
                                     {"text": "The parties acknowledge this section.", "tag": None}]})
    return {"title": "Agreement", "blocks": blocks,
            "weakened": {tag: f"Vendor may consider {word[:3]}xx work." for word, tag in WORDS.items()}}


def _seed() -> dict:
    return {"base": "toy", "profile": "toy pair", "derivation": "paraphrased",
            "generator_llm": "claude-code-agent:test", "parties": {"covered_entity": "A", "business_associate": "B"},
            "presentations": {"template": _presentation(False), "vendor_paper": _presentation(True)}}


@pytest.fixture(autouse=True)
def _no_length_limits(monkeypatch):
    monkeypatch.setattr(seed_baas, "WORDS", {"template": (0, 10**6), "vendor_paper": (0, 10**6)})


def test_contract_meets_needs_a_binding_term():
    e = {"signature": ["subcontractor"]}
    assert meets("Vendor shall bind each subcontractor.", e)
    assert meets("Vendor may only use a subcontractor that signs a written agreement.", e)
    assert not meets("Subcontractor means a person to whom Vendor delegates a function.", e)
    assert not meets('"Subcontractor" shall have the meaning given in 45 CFR 160.103.', e)


def test_valid_seed_passes_and_checker_catches_problems():
    assert check_seed(_seed(), ELEMENTS) == []
    bad = _seed()
    pres = bad["presentations"]["vendor_paper"]
    pres["weakened"]["U1#E1"] = "Vendor shall do alpha work anyway."  # twin still meets
    pres["blocks"][0]["sentences"].append({"text": "Vendor shall keep gamma records.", "tag": None})  # untagged leak
    pres["blocks"][1]["sentences"].append({"text": "This section is reserved.", "tag": None})  # placeholder
    probs = "\n".join(check_seed(bad, ELEMENTS))
    assert "twin still meets U1#E1" in probs
    assert "untagged also meets U2#E1" in probs
    assert "placeholder" in probs


def test_variants_render_both_presentations_with_identical_labels_and_no_leaks():
    seed = _seed()
    pattern = ["covered", "partial", "gap"]
    seen_mutations = set()
    for i in range(8):
        mp = plan_manual("toy", 0, i, ELEMENTS["groups"], pattern, allow_hollow=False)
        assert all(p.mutation != "hollow" for p in mp.units.values())
        for pres in ("template", "vendor_paper"):
            text, rendered = render(seed["presentations"][pres], mp, "CANARY")
            assert audit(ELEMENTS, mp, rendered) == []
            for word, tag in WORDS.items():
                unit, _, el = tag.partition("#")
                plan = mp.units[unit]
                removed = plan.mutation == "remove_section" or (plan.element == el and plan.mutation != "intact")
                assert (f"do {word} work" in text) != removed, (i, pres, tag, plan)
            assert "reserved" not in text.lower() and "[" not in text
        seen_mutations |= {p.mutation for p in mp.units.values()}
    assert {"remove_section", "intact"} <= seen_mutations


def test_template_clauses_reletter_when_one_is_removed():
    seed = _seed()
    for i in range(8):
        mp = plan_manual("toy", 0, i, ELEMENTS["groups"], ["gap", "covered"], allow_hollow=False)
        text, _ = render(seed["presentations"]["template"], mp, "C")
        for article in text.split("\n## ")[1:]:  # letters restart under each article
            letters = [ln[1] for ln in article.splitlines() if ln.startswith("(") and ln[2] == ")"]
            assert letters == [chr(ord("a") + k) for k in range(len(letters))]


def test_doc_ids_hide_the_presentation():
    ids = {_doc_id(f"x-{i:02d}", "template")[-1] for i in range(20)}
    assert ids == {"a", "b"}
    assert _doc_id("x-01", "template") != _doc_id("x-01", "vendor_paper")


# The real elements file: drafted clauses meet their element, realistic weakenings do not, and no
# clause meets another unit's element. Keeps signature edits honest.
REAL = [
    ("164.504(e)(2)(i)#E1",
     "Business Associate may only use or disclose protected health information as necessary to perform the services set forth in the Service Agreement.",
     "Business Associate may use and disclose protected health information in connection with its business operations."),
    ("164.504(e)(2)(ii)(A)#E1",
     "Business Associate shall not use or disclose protected health information other than as permitted or required by this Agreement or as required by law.",
     "Business Associate will handle protected health information as reasonably necessary for its business purposes."),
    ("164.504(e)(2)(ii)(B)#E2",
     "Business Associate shall comply with Subpart C of 45 CFR Part 164 with respect to electronic protected health information.",
     "Business Associate shall follow its written information security program for electronic records."),
    ("164.314(a)(2)(i)(C)#E1",
     "Business Associate shall report to Covered Entity any security incident of which it becomes aware.",
     "Business Associate shall report to Covered Entity any confirmed Breach that it determines requires notification."),
    ("164.410(b)#E2",
     "In no case shall Business Associate notify Covered Entity of a breach later than ten (10) business days after discovery.",
     "Business Associate shall notify Covered Entity of a breach no later than ninety (90) days after discovery."),
    ("164.504(e)(2)(ii)(D)#E1",
     "Business Associate shall ensure that any subcontractors that create, receive, maintain, or transmit protected health information on its behalf agree to the same restrictions and conditions that apply to Business Associate.",
     "Business Associate shall ensure that its material subcontractors agree to substantially similar confidentiality terms."),
    ("164.504(e)(2)(ii)(F)#E1",
     "Business Associate shall make protected health information in a designated record set available for amendment.",
     "Business Associate shall cooperate with record corrections."),
    ("164.504(e)(2)(ii)(I)#E1",
     "Business Associate shall make its internal practices, books, and records available to the Secretary for purposes of determining compliance with the HIPAA Rules.",
     "Business Associate shall make its internal practices, books, and records available to Covered Entity upon reasonable notice."),
    ("164.504(e)(2)(ii)(J)#E2",
     "Business Associate shall retain no copies of the protected health information.",
     "Business Associate may retain archival copies in accordance with its standard records retention schedule."),
]


@pytest.mark.parametrize("tag,good,weak", REAL)
def test_real_signatures(tag, good, weak):
    tags = tags_of(load_elements(ROOT / "data/seeds/baa_elements.v1.yaml"))
    assert meets(good, tags[tag])
    assert not meets(weak, tags[tag])
    assert [t for t, e in tags.items() if t != tag and meets(good, e)] == []
