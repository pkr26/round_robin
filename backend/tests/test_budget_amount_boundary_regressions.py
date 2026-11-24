from decimal import Decimal

import pytest

from round_robin.orchestration.budget import BudgetDenied, amount


@pytest.mark.parametrize("value, expected", [("0", "0"), ("0.5", "0.5"), ("1.25", "1.25")])
def test_valid_amounts_preserve_value(value, expected):
    assert amount(value) == Decimal(expected)


@pytest.mark.parametrize("value", ["-0.01", "Infinity", "-Infinity", "not-money"])
def test_invalid_amounts_are_denied(value):
    with pytest.raises(BudgetDenied, match="^invalid_money$"):
        amount(value)
