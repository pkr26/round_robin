"""Break-even results preserve the priced inputs and their provenance."""

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import break_even
from round_robin.schemas.briefs import Option


def test_break_even_input_evidence_includes_only_priced_components_with_correct_units():
    a, b = sample_brief().options[:2]
    ignored = {**a.cost_components[0].model_dump(), "id": "ignored_cost", "label": "Not applicable", "status": "not_applicable"}
    a = Option.model_validate({**a.model_dump(), "cost_components": [*[c.model_dump() for c in a.cost_components], ignored]})

    result = break_even(a, b, "USD")

    assert [(i.label, i.amount, i.unit, i.source_ref) for i in result.inputs] == [
        (f"{option.label}: {component.label}", component.amount,
         "USD/month" if component.frequency == "monthly" else "USD", component.source_ref)
        for option in (a, b) for component in option.cost_components
        if component.status == "included"
    ]
    assert result.id == "calc_break_even"
    assert result.unit == "months"
    assert result.flags == []


def test_break_even_input_labels_are_bounded_for_combined_evidence():
    a, b = sample_brief().options[:2]
    a_data = a.model_dump()
    a_data["label"] = "A" * 241
    a_data["cost_components"][0]["label"] = "C" * 259
    a = Option.model_validate(a_data)

    result = break_even(a, b, "USD")

    assert result.inputs[0].label == "A" * 239 + "…: " + "C" * 257 + "…"
    assert len(result.inputs[0].label) == 500


def test_break_even_enforces_rendered_output_length_boundary():
    from round_robin.research.calculator import CalculationError
    import pytest

    a, b = sample_brief().options[:2]
    a_data, b_data = a.model_dump(), b.model_dump()
    for component in a_data["cost_components"] + b_data["cost_components"]:
        component["amount"] = "0"
    a_data["cost_components"][1].update(amount="3000000000", quantity="1000000000")
    b_data["cost_components"][0].update(amount="0.000001", quantity="0.000001")
    a, b = Option.model_validate(a_data), Option.model_validate(b_data)
    result = break_even(a, b, "USD")
    assert result.output == "0." + "0" * 30 + "3" * 64
    assert len(result.output) == 96
    a_data["cost_components"][1]["amount"] = "30000000000"
    a = Option.model_validate(a_data)
    with pytest.raises(CalculationError, match="exceeds output bounds"):
        break_even(a, b, "USD")


def test_break_even_allows_zero_and_fractional_positive_months():
    a, b = sample_brief().options[:2]
    data = b.model_dump()
    data["cost_components"][0]["amount"] = "1000"
    assert break_even(a, Option.model_validate(data), "USD").output == "0"
    data["cost_components"][0]["amount"] = "1001"
    result = break_even(a, Option.model_validate(data), "USD")
    assert result.output == "0.0" + "3" * 64
    assert result.flags == ["division rounded at Decimal precision 64"]


def test_break_even_explains_negative_crossing():
    from round_robin.research.calculator import CalculationError
    import pytest

    a, b = sample_brief().options[:2]
    data = b.model_dump()
    data["cost_components"][0]["amount"] = "999"
    with pytest.raises(CalculationError, match="(?i)nonnegative"):
        break_even(a, Option.model_validate(data), "USD")
