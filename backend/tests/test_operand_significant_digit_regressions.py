"""Calculator operands enforce significant and fractional digit limits."""

import pytest
from pydantic import ValidationError
from round_robin.research.calculator import Operand


def test_eighteen_significant_digits_are_accepted():
    value = "999999999999.999999"
    assert Operand(amount=value, unit="USD", kind="money").amount == value


def test_nineteen_significant_digits_are_rejected_even_at_maximum():
    with pytest.raises(ValidationError):
        Operand(amount="1000000000000.000000", unit="USD", kind="money")


def test_fractional_limit_is_independent_of_significant_digit_limit():
    with pytest.raises(ValidationError):
        Operand(amount="1.1234567", unit="USD", kind="money")
