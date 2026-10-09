from collections import Counter

from rightsize_pack_compliance.gen.manuals import audit, plan_manual, render_manual
from rightsize_pack_compliance.gen.seed_manuals import _check_group

ELEMENTS = {
    "version": 1,
    "groups": [
        {"code": "G1", "topic": "t1", "units": [
            {"code": "U1", "elements": [
                {"id": "E1", "signature": ["alpha"]},
                {"id": "E2", "signature": ["beta"]},
            ]},
            {"code": "U2", "elements": [{"id": "E1", "signature": ["gamma"]}]},
        ]},
        {"code": "G2", "topic": "t2", "units": [
            {"code": "U3", "elements": [{"id": "E1", "signature": ["delta"]}]},
        ]},
        {"code": "G3", "topic": "t3", "units": [
            {"code": "U4", "elements": [
                {"id": "E1", "signature": ["epsilon"]},
                {"id": "E2", "signature": ["zeta"]},
            ]},
        ]},
    ],
}


def _section(unit, tagged):
    return {
        "unit": unit, "heading": f"About {unit}",
        "sentences": [{"text": "Context sentence.", "element": None}]
        + [{"text": f"We always do {word}.", "element": eid} for eid, word in tagged],
        "weakened": {eid: "We may consider doing something." for eid, _ in tagged},
    }


SEED = {
    "front": {
        "title": "Manual", "preamble": ["Purpose."],
        "distractors": [{"heading": f"D{i}", "body": "Parking rules."} for i in range(6)],
        "near_misses": [{"heading": f"N{i}", "body": "Be aware."} for i in range(3)],
    },
    "groups": {
        "G1": {"heading": "Group one", "intro": "Intro.", "sections": [
            _section("U1", [("E1", "alpha"), ("E2", "beta")]), _section("U2", [("E1", "gamma")])]},
        "G2": {"heading": "Group two", "intro": "Intro.", "sections": [_section("U3", [("E1", "delta")])]},
        "G3": {"heading": "Group three", "intro": "Intro.", "sections": [
            _section("U4", [("E1", "epsilon"), ("E2", "zeta")])]},
    },
}
PATTERN = ["covered", "covered", "covered", "covered", "partial", "partial", "partial", "gap", "gap", "gap"]


def test_group_labels_cycle_and_balance():
    labels = Counter()
    for i in range(10):
        mp = plan_manual("b", 0, i, ELEMENTS["groups"], PATTERN)
        labels.update(mp.groups.values())
    # G2 is one unit with one element, so its "partial" draws are re-labelled
    assert labels["covered"] >= 10 and labels["gap"] >= 9 and labels["partial"] >= 5


def test_mutations_match_labels():
    for i in range(10):
        mp = plan_manual("b", 0, i, ELEMENTS["groups"], PATTERN)
        for g in ELEMENTS["groups"]:
            label = mp.groups[g["code"]]
            muts = [mp.units[u["code"]].mutation for u in g["units"]]
            if label == "covered":
                assert set(muts) == {"intact"}
            elif label == "gap":
                assert set(muts) <= {"remove_section", "hollow"}
            else:
                statuses = {mp.units[u["code"]].status for u in g["units"]}
                assert any(m != "intact" for m in muts)
                assert statuses not in ({"covered"}, {"gap"})


def test_partial_unit_keeps_some_elements():
    for i in range(20):
        mp = plan_manual("b", 0, i, ELEMENTS["groups"], PATTERN)
        p = mp.units["U4"]
        if p.status == "partial":
            _, units = render_manual(SEED, ELEMENTS, mp, "CANARY")
            kept = units["U4"]
            assert ("epsilon" in kept) != ("zeta" in kept)


def test_render_hides_tags_and_renumbers():
    for i in range(10):
        mp = plan_manual("b", 0, i, ELEMENTS["groups"], PATTERN)
        text, _ = render_manual(SEED, ELEMENTS, mp, "CANARY")
        assert "CANARY" in text
        assert '"element"' not in text and "E1" not in text
        nums = [int(line.split(".")[0][3:]) for line in text.splitlines() if line.startswith("## ")]
        assert nums == list(range(1, len(nums) + 1))


def test_audit_flags_element_met_elsewhere():
    seed = {**SEED, "groups": dict(SEED["groups"])}
    leaky = _section("U3", [("E1", "delta")])
    leaky["sentences"].append({"text": "Also alpha and beta.", "element": None})
    seed["groups"]["G2"] = {"heading": "G2", "intro": "Intro.", "sections": [leaky]}
    found = False
    for i in range(10):
        mp = plan_manual("b", 0, i, ELEMENTS["groups"], PATTERN)
        if mp.units["U1"].mutation != "intact" and mp.units["U3"].mutation not in ("remove_section", "hollow"):
            _, units = render_manual(seed, ELEMENTS, mp, "C")
            leaks = audit(ELEMENTS, mp, units)
            assert any(lk["unit"] == "U1" and lk["found_in"] == "U3" for lk in leaks)
            found = True
    assert found


def test_seed_checker_catches_bad_replies():
    g = ELEMENTS["groups"][0]
    good = SEED["groups"]["G1"]
    assert _check_group(good, g) == []
    bad = {**good, "sections": [
        {**good["sections"][0], "weakened": {"E1": "still alpha", "E2": "vague"}}, good["sections"][1]]}
    assert any("weakened twin still meets" in p for p in _check_group(bad, g))
    missing = {**good, "sections": [good["sections"][0]]}
    assert any("missing section U2" in p for p in _check_group(missing, g))


def test_implied_elements_are_never_dropped():
    groups = [{"code": "G", "topic": "t", "units": [{"code": "U", "elements": [
        {"id": "E1", "signature": ["a"], "implied_by": ["E2"]},
        {"id": "E2", "signature": ["b"]},
    ]}]}]
    for i in range(30):
        mp = plan_manual("b", 0, i, groups, PATTERN)
        if mp.units["U"].status == "partial":
            assert mp.units["U"].element == "E2"


def test_seed_checker_survives_malformed_replies():
    g = ELEMENTS["groups"][0]
    bad = {"heading": "h", "intro": "i", "sections": [
        {"unit": "U1", "heading": "x", "sentences": [None, {"text": None}], "weakened": None}]}
    assert _check_group(bad, g)  # reports problems, does not raise
