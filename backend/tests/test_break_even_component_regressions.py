"""Break-even calculations must account for all priced components."""

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import break_even
from round_robin.schemas.briefs import CostComponent, Option


def _with_components(option, *components):
    return Option.model_validate({
        **option.model_dump(),
        "cost_components": [
            *[item.model_dump() for item in option.cost_components],
            *[item.model_dump() for item in components],
        ],
    })


def _component(template, *, id, kind, frequency, amount, quantity="1"):
    return CostComponent.model_validate({
        **template.model_dump(),
        "id": id,
        "kind": kind,
        "frequency": frequency,
        "amount": amount,
        "quantity": quantity,
    })


def test_break_even_counts_quantities_and_multiple_monthly_charges():
    first, second = sample_brief().options[:2]
    template = first.cost_components[0]
    first = _with_components(
        first,
        _component(template, id="extra_upfront", kind="upfront", frequency="once", amount="100", quantity="2"),
        _component(template, id="extra_monthly", kind="recurring", frequency="monthly", amount="10", quantity="2"),
    )

    calculation = break_even(first, second, "USD")

    assert calculation.formula == "(1600 − 1200) ÷ (70 − 20)"
    assert calculation.output == "8"


def test_break_even_subtracts_once_and_monthly_discounts():
    first, second = sample_brief().options[:2]
    template = first.cost_components[0]
    first = _with_components(
        first,
        _component(template, id="upfront_discount", kind="discount", frequency="once", amount="100"),
        _component(template, id="monthly_discount", kind="discount", frequency="monthly", amount="10"),
    )

    calculation = break_even(first, second, "USD")

    assert calculation.formula == "(1600 − 900) ÷ (40 − 20)"
    assert calculation.output == "35"
