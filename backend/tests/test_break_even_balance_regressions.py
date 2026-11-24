"""Regression coverage for break-even balances and component ordering."""

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import break_even
from round_robin.schemas.briefs import CostComponent, Option


def test_break_even_formula_uses_actual_upfront_and_monthly_totals():
    first, second = sample_brief().options[:2]

    calculation = break_even(first, second, "USD")

    assert calculation.output == "20"
    assert calculation.formula == "(1600 − 1000) ÷ (50 − 20)"


def test_break_even_counts_included_costs_after_non_applicable_component():
    first, second = sample_brief().options[:2]
    ignored = CostComponent.model_validate({
        **first.cost_components[0].model_dump(),
        "id": "cost_not_applicable",
        "status": "not_applicable",
    })
    first = Option.model_validate({
        **first.model_dump(),
        "cost_components": [ignored.model_dump(), *[component.model_dump() for component in first.cost_components]],
    })

    calculation = break_even(first, second, "USD")

    assert calculation.output == "20"
    assert calculation.formula == "(1600 − 1000) ÷ (50 − 20)"
    assert len(calculation.inputs) == len(first.cost_components) + len(second.cost_components) - 1
