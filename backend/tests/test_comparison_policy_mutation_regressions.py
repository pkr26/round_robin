"""Compare supplied cost evidence without inventing missing totals or constraints."""

from datetime import date

import pytest
from round_robin.fixtures import sample_brief
from round_robin.orchestration.reporting import comparison_data
from round_robin.schemas.briefs import DraftBrief

CATEGORIES = ("upfront", "installation", "recurring", "maintenance", "tax_delivery")


def complete_body():
    body = sample_brief().model_dump(mode="json")
    body["options"] = body["options"][:1]
    components = body["options"][0]["cost_components"]
    for category in CATEGORIES:
        if not any(component["kind"] == category for component in components):
            components.append({
                "id": f"extra_{category}",
                "label": category,
                "kind": category,
                "amount": "0",
                "currency": "USD",
                "frequency": "once",
                "status": "not_applicable",
                "source_ref": f"quote_{category}",
            })
    return body


@pytest.mark.parametrize("category", CATEGORIES)
def test_omitted_cost_category_cannot_produce_a_complete_total(category):
    body = complete_body()
    body["options"][0]["cost_components"] = [
        item for item in body["options"][0]["cost_components"] if item["kind"] != category
    ]
    brief = DraftBrief.model_validate(body)
    comparisons, calculations = comparison_data(brief, as_of=date(2026, 1, 1))
    option, calculation = comparisons[0], calculations[0]
    assert option["total"] is None and calculation["output"] is None
    assert option["known_subtotal"] == calculation["known_subtotal"]
    assert any(category.replace("_", " ") in gap for gap in option["missing_components"])
    assert option["missing_components"] == calculation["missing_components"]
    result = option["constraint_results"][0]
    assert result["result"] == "unknown"
    assert result["basis_refs"] == []
    assert result["method"] == "fixture_assessment"


@pytest.mark.parametrize(
    ("operator", "threshold", "expected"),
    [
        ("lte", "1599", "fail"), ("lte", "1600", "pass"), ("lte", "1601", "pass"),
        ("gte", "1599", "pass"), ("gte", "1600", "pass"), ("gte", "1601", "fail"),
        ("eq", "1599", "fail"), ("eq", "1600", "pass"), ("eq", "1601", "fail"),
    ],
)
def test_complete_cost_constraint_boundary_and_calculation_provenance(operator, threshold, expected):
    body = complete_body()
    body["hard_requirements"][0].update(operator=operator, threshold=threshold)
    body["hard_requirements"].append({
        "id": "requirement_lead_time", "description": "Delivery within 1600 days",
        "origin": "fictional", "metric": "lead_time_days", "operator": "lte", "threshold": "1600",
    })
    brief = DraftBrief.model_validate(body)
    comparisons, calculations = comparison_data(brief, as_of=date(2026, 1, 1))
    option, calculation = comparisons[0], calculations[0]
    assert option["total"] == calculation["output"] == "1600.00"
    assert option["missing_components"] == []
    cost, manual, lead_time = option["constraint_results"]
    assert cost["result"] == expected
    assert cost["basis_refs"] == [calculation["id"]] == [option["calculation_id"]]
    assert cost["method"] == "deterministic_calculation"
    for other in (manual, lead_time):
        assert other["result"] == "unknown"
        assert other["basis_refs"] == []
        assert other["method"] == "fixture_assessment"


def test_cost_comparisons_explain_that_entered_prices_are_not_verified():
    brief = sample_brief()
    comparisons, _ = comparison_data(brief, as_of=date(2026, 1, 1))

    assert len(comparisons) == len(brief.options) > 0
    for comparison in comparisons:
        assert comparison["caveat"] == (
            "Entered costs are arithmetic inputs, not independently verified supplier facts."
        )
