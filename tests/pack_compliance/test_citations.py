import pytest
from rightsize_pack_compliance.citations import best_grade, find_in_text, grade, normalize, parse

CANON = "164.312(a)(2)(iv)"


@pytest.mark.parametrize(
    "raw",
    [
        "164.312(a)(2)(iv)",
        "§ 164.312(a)(2)(iv)",
        "§164.312(a)(2)(iv)",
        "45 CFR 164.312(a)(2)(iv)",
        "45 C.F.R. § 164.312(a)(2)(iv)",
        "45 CFR § 164.312 (a)(2)(iv)",
        "164.312 (a) (2) (iv)",
        "  164.312(a)(2)(iv)  ",
        "Sec. 164.312(a)(2)(iv)",
        "section 164.312(a)(2)(iv)",
    ],
)
def test_spellings_normalize(raw):
    assert normalize(raw) == CANON


@pytest.mark.parametrize("raw", [None, "", "access control", "HIPAA", "(a)(2)(iv)"])
def test_unparseable(raw):
    assert normalize(raw) is None


def test_section_only():
    assert normalize("45 CFR 164.308") == "164.308"


def test_parent_and_ancestry():
    c = parse(CANON)
    assert c.parent().canonical == "164.312(a)(2)"
    assert parse("164.312(a)").is_ancestor_of(c)
    assert not c.is_ancestor_of(parse("164.312(a)"))
    assert not parse("164.312(e)").is_ancestor_of(c)


@pytest.mark.parametrize(
    "hit,target,expected",
    [
        (CANON, CANON, 3),
        ("§ 164.312(a)(2)(iv)", CANON, 3),
        ("164.312(a)(2)", CANON, 1),  # ancestor
        ("164.312", CANON, 1),  # ancestor
        ("164.312(a)(1)", CANON, 1),  # same standard
        ("164.312(e)(2)(ii)", CANON, 0),  # the classic hard negative
        ("164.310(d)(2)(iv)", "164.308(a)(7)(ii)(A)", 0),
        ("164.308(a)(1)(ii)(A)", "164.308(a)(1)", 2),  # descendant
        ("164.316(b)(2)(i)", "164.316(b)", 2),
        (None, CANON, 0),
        ("garbage", CANON, 0),
    ],
)
def test_grades(hit, target, expected):
    assert grade(hit, target) == expected


def test_best_grade_over_targets():
    assert best_grade("164.312(e)(2)(ii)", [CANON, "164.312(e)(1)"]) == 1
    assert best_grade("164.312(e)(2)(ii)", []) == 0


def test_find_in_text():
    text = (
        "See 45 CFR § 164.312(a)(2)(iv) and also §164.312(e)(2)(ii); "
        "the standard at 164.312(a)(2)(iv) repeats. Part 160.103 defines terms."
    )
    assert find_in_text(text) == [CANON, "164.312(e)(2)(ii)", "160.103"]
