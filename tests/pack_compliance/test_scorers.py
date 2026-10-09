import pytest
from rightsize_core.schema import Provenance, Task
from rightsize_pack_compliance.models import Assessment, ControlPrediction, Hit, RetrievalPrediction
from rightsize_pack_compliance.scorers import (
    aggregate_control,
    aggregate_retrieval,
    control_floors,
    score_control,
    score_retrieval,
)

PROV = Provenance(generator="test")


def rtask(target="164.312(a)(2)(iv)", negs=("164.312(e)(2)(ii)",)):
    return Task(
        id="r1", suite="t", pack="compliance", task_type="retrieval", split="public",
        inputs={"query": "q", "k": 10},
        expected={"relevant": [{"citation": target, "grade": 3}], "hard_negatives": list(negs),
                  "corpus_parts": ["160", "164"]},
        provenance=PROV, canary="c",
    )


def hits(*cites, source="field"):
    return RetrievalPrediction(hits=[Hit(citation=c, citation_source=source) for c in cites])


def test_retrieval_exact_first():
    [u] = score_retrieval(rtask(), hits("§ 164.312(a)(2)(iv)", "164.312(b)"), "predicted")
    assert u.values["ndcg@10"] == pytest.approx(1.0)
    assert u.values["recall@1"] == 1.0
    assert u.values["mrr"] == 1.0
    assert u.values["confusion@k"] == 0.0


def test_retrieval_hard_negative_first_is_confusion():
    [u] = score_retrieval(rtask(), hits("164.312(e)(2)(ii)", "164.312(a)(2)(iv)"), "predicted")
    assert u.values["confusion@k"] == 1.0
    assert u.values["mrr"] == 0.5
    assert u.values["recall@1"] == 0.0


def test_retrieval_partial_credit_only():
    [u] = score_retrieval(rtask(), hits("164.312(a)(2)"), "predicted")
    assert 0 < u.values["ndcg@10"] < 1
    assert u.values["recall@5"] == 0.0
    assert u.values["recall@5_lenient"] == 1.0


def test_retrieval_duplicate_exact_hits_do_not_exceed_one():
    [u] = score_retrieval(rtask(), hits("164.312(a)(2)(iv)", "164.312(a)(2)(iv)", "164.312(a)(1)"), "predicted")
    assert u.values["ndcg@10"] <= 1.0


def test_retrieval_out_of_corpus_and_inferred():
    [u] = score_retrieval(rtask(), hits("42 CFR 482.12", "164.312(a)(2)(iv)", source="inferred"), "predicted")
    assert u.values["out_of_corpus_share"] == 0.5
    assert u.values["inferred_share"] == 1.0


def test_retrieval_errors_are_not_scored_as_misses():
    units = score_retrieval(rtask(), None, "error") + score_retrieval(rtask(), hits("164.312(a)(2)(iv)"), "predicted")
    agg = aggregate_retrieval(units)
    assert agg["error_rate"] == 0.5
    assert agg["ndcg@10"] == pytest.approx(1.0)


def ctask():
    units = {
        "164.312(a)(2)(i)": {"status": "covered"},
        "164.312(a)(2)(iv)": {"status": "partial", "mutation": "weaken"},
        "164.312(b)": {"status": "gap", "mutation": "remove_section"},
    }
    groups = {
        "164.312(a)": {"status": "partial", "members": ["164.312(a)(2)(i)", "164.312(a)(2)(iv)"]},
        "164.312(b)": {"status": "gap", "members": ["164.312(b)"]},
    }
    return Task(
        id="m1", suite="t", pack="compliance", task_type="control_classification", split="public",
        inputs={"document_id": "manual_1", "document": "...", "document_sha256": "x", "units": []},
        expected={"units": units, "groups": groups}, provenance=PROV, canary="c",
    )


def pred(**statuses):
    return ControlPrediction(assessments=[Assessment(control_code=c, status=s) for c, s in statuses.items()])


def test_control_oracle_scores_one():
    t = ctask()
    p = ControlPrediction(assessments=[
        Assessment(control_code=c, status=e["status"]) for c, e in t.expected["units"].items()
    ])
    agg = aggregate_control(score_control(t, p, "predicted"))
    assert agg["spec.macro_f1"] == 1.0
    assert agg["group.macro_f1"] == 1.0  # derived from members
    assert agg["spec.coverage"] == 1.0


def test_control_missing_units_are_no_prediction_not_wrong():
    agg = aggregate_control(score_control(ctask(), pred(**{"164.312(b)": "gap"}), "predicted"))
    assert agg["spec.no_prediction_rate"] == pytest.approx(2 / 3)
    assert agg["spec.macro_f1"] == 1.0  # the one answer given was right


def test_control_extras_counted_not_penalized():
    p = pred(**{"164.312(b)": "gap", "164.999(z)": "gap"})
    units = score_control(ctask(), p, "predicted")
    assert units[0].extra["extras"] == 1


def test_control_abstention_two_views():
    t = ctask()
    p = ControlPrediction(assessments=[
        Assessment(control_code="164.312(a)(2)(i)", status="covered"),
        Assessment(control_code="164.312(a)(2)(iv)", status="gap", abstained=True),
        Assessment(control_code="164.312(b)", status="gap"),
    ])
    agg = aggregate_control(score_control(t, p, "predicted"))
    assert agg["spec.abstain_rate"] == pytest.approx(1 / 3)
    assert agg["spec.macro_f1"] == 1.0  # selective: abstained excluded
    assert agg["spec.macro_f1_as_shipped"] < 1.0  # shipped as gap, gold partial


def test_control_errors_kept_separate():
    agg = aggregate_control(score_control(ctask(), None, "error"))
    assert agg["spec.error_rate"] == 1.0
    assert "spec.macro_f1" not in agg


def test_group_level_answer_preferred_over_derivation():
    p = pred(**{"164.312(a)": "covered", "164.312(b)": "gap"})
    units = {u.unit_id.split("::")[1]: u for u in score_control(ctask(), p, "predicted") if u.level == "group"}
    assert units["164.312(a)"].pred == "covered"
    assert "derived" not in units["164.312(a)"].extra


def test_floors():
    f = control_floors([ctask()])
    assert f["always_covered"]["spec.accuracy"] == pytest.approx(1 / 3)
    assert f["always_partial"]["group.accuracy"] == pytest.approx(1 / 2)


def test_coarser_group_code_maps_to_its_group():
    t = ctask()
    # "164.312" contains both groups -> ambiguous -> extra; "164.312(a)" holds only one group.
    p = pred(**{"164.312(a)": "partial", "164.312": "gap"})
    units = score_control(t, p, "predicted")
    g = {u.unit_id.split("::")[1]: u for u in units if u.level == "group"}
    assert g["164.312(a)"].pred == "partial"
    assert units[0].extra["extras"] == 1
