from decimal import Decimal, localcontext

import pytest

from round_robin.orchestration.reporting import comparison_data
from round_robin.reports.insights import decision_insights
from round_robin.schemas.briefs import Constraint, DraftBrief, Preference


def brief(requirements=(), preferences=()):
    return DraftBrief(revision=1, title="Fixture", question="Compare", goal="Decide", currency="USD", hard_requirements=list(requirements), preferences=list(preferences))


def requirement(identity, *, metric="manual", operator="manual", threshold=None):
    return Constraint(id=identity, description="Requirement", origin="user_supplied", metric=metric, operator=operator, threshold=threshold)


def comparison(identity, total, results, missing=()):
    return {"option_id": identity, "total": total, "missing_components": list(missing), "constraint_results": results}


def result(identity, status, disputed=False):
    return {"requirement_id": identity, "result": status, "disputed": disputed, "basis_refs": ["source_test"]}


def test_frontier_only_compares_matching_applicability_and_retains_ties():
    policy = brief([requirement("req1"), requirement("req2")])
    same = [result("req1", "pass"), result("req2", "not_applicable")]
    other = [result("req2", "pass"), result("req1", "pass")]
    comparisons = [comparison("cheap", "100.00", same), comparison("tied", "100.00", list(reversed(same))), comparison("expensive", "100.01", same), comparison("different", "200.00", other)]
    insights = decision_insights(policy, comparisons)
    assert insights["cost_frontier_option_ids"] == ["cheap", "different", "tied"]
    assert insights["dominated_option_ids"] == ["expensive"]
    assert insights["unresolved_option_ids"] == []
    assert insights["cost_comparison_blocker"] == "different_applicability"
    assert insights["tied_lowest_cost_option_ids"] == []


def test_incomplete_unknown_failed_and_disputed_options_are_not_ranked():
    policy = brief([requirement("req1")])
    invalid = [comparison("missing_total", None, [result("req1", "pass")]), comparison("missing_component", "0.00", [result("req1", "pass")], ["Confirm tax"]), comparison("unassessed", "0.00", []), comparison("unknown", "0.00", [result("req1", "unknown")]), comparison("failed", "0.00", [result("req1", "fail")]), comparison("disputed", "0.00", [result("req1", "pass", True)])]
    for start in (0, 3):
        insights = decision_insights(policy, invalid[start:start + 3] + [comparison("valid", "1.00", [result("req1", "pass")])])
        assert insights["cost_frontier_option_ids"] == ["valid"]
        assert insights["dominated_option_ids"] == []
        assert insights["unresolved_option_ids"] == sorted(item["option_id"] for item in invalid[start:start + 3] if item["option_id"] != "failed")
        assert insights["failed_option_ids"] == (["failed"] if start == 3 else [])


def test_zero_requirements_and_no_comparisons_do_not_invent_rankings():
    policy = brief()
    assert decision_insights(policy, [comparison("zero", "0.00", []), comparison("other", "1.00", [])])["cost_frontier_option_ids"] == ["zero"]
    empty = decision_insights(policy, [])
    assert empty["cost_frontier_option_ids"] == empty["dominated_option_ids"] == empty["unresolved_option_ids"] == empty["sensitivity"] == []


def test_signed_threshold_margins_and_preference_order():
    requirements = [requirement("ceiling", metric="total_cost", operator="lte", threshold="100.00"), requirement("floor", metric="total_cost", operator="gte", threshold="100.00"), requirement("delivery", metric="lead_time_days", operator="lte", threshold="5.00"), requirement("manual")]
    preferences = [Preference(id="second", description="Second", rank=2, origin="user_supplied"), Preference(id="first", description="First", rank=1, origin="user_supplied")]
    insights = decision_insights(brief(requirements, preferences), [comparison("over", "100.01", []), comparison("under", "99.99", []), comparison("unavailable", None, [])])
    margins = {(item["option_id"], item["requirement_id"]): (item["margin"], item["unit"]) for item in insights["sensitivity"]}
    assert margins == {("over", "ceiling"): ("-0.01", "USD"), ("over", "floor"): ("0.01", "USD"), ("under", "ceiling"): ("0.01", "USD"), ("under", "floor"): ("-0.01", "USD")}
    for item in insights["sensitivity"]:
        operator = "lte" if item["requirement_id"] == "ceiling" else "gte"
        assert item["scope"] == f"Signed margin for {operator} using supplied costs; not a probability or forecast."
    assert insights["preference_order"] == ["first", "second"]


def test_threshold_margin_is_not_rounded_by_caller_decimal_context():
    policy = brief([requirement("ceiling", metric="total_cost", operator="lte", threshold="123456789.99")])
    with localcontext() as context:
        context.prec = 4
        sensitivity = decision_insights(policy, [comparison("option", "123456789.98", [])])["sensitivity"]
    assert Decimal(sensitivity[0]["margin"]) == Decimal("0.01")


@pytest.mark.parametrize(("total", "constraint_result", "margin"), [
    ("99.99", "fail", "-0.01"),
    ("100.00", "pass", "0.00"),
    ("100.01", "fail", "-0.01"),
])
def test_exact_cost_target_has_zero_margin_only_when_it_passes(total, constraint_result, margin):
    policy = DraftBrief.model_validate({
        **brief([requirement("exact_cost", metric="total_cost", operator="eq", threshold="100.00")]).model_dump(),
        "horizon_months": 1,
        "options": [{
            "id": "option", "label": "Supplied quote", "provenance": "user_supplied",
            "cost_components": [
                {"id": kind, "label": kind, "kind": kind, "amount": total if kind == "upfront" else "0.00",
                 "currency": "USD", "frequency": "once", "source_ref": "evidence_costs"}
                for kind in ("upfront", "installation", "recurring", "maintenance", "tax_delivery")
            ],
        }],
    })
    comparisons, _ = comparison_data(policy)
    assert comparisons[0]["constraint_results"][0]["result"] == constraint_result
    sensitivity = decision_insights(policy, comparisons)["sensitivity"]
    assert len(sensitivity) == 1
    assert sensitivity[0]["margin"] == margin
    assert sensitivity[0]["scope"] == "Signed margin for eq using supplied costs; not a probability or forecast."
