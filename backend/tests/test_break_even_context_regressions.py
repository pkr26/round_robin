"""Break-even division retains its specified precision and guards equal rates."""

from decimal import localcontext

import pytest

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import CalculationError, break_even
from round_robin.schemas.briefs import Option


def _rates(option, upfront, monthly):
    values = {"upfront": upfront, "recurring": monthly, "installation": "0"}
    components = []
    for component in option.cost_components:
        data = component.model_dump()
        data["amount"] = values[component.kind]
        components.append(data)
    return Option.model_validate({**option.model_dump(), "cost_components": components})


def test_break_even_division_uses_64_digits_independent_of_caller_context():
    a, b = sample_brief().options[:2]
    a, b = _rates(a, "0", "3"), _rates(b, "2", "0")
    with localcontext() as caller:
        caller.prec = 6
        result = break_even(a, b, "USD")
    assert result.output == "0." + "6" * 63 + "7"
    assert result.flags == ["division rounded at Decimal precision 64"]


def test_break_even_guards_equal_rates_but_allows_difference_of_one():
    a, b = sample_brief().options[:2]
    a, b = _rates(a, "0", "1"), _rates(b, "2", "0")
    assert break_even(a, b, "USD").output == "2"
    b = _rates(b, "2", "1")
    with pytest.raises(CalculationError, match="No unique break-even month exists for equal recurring costs"):
        break_even(a, b, "USD")
