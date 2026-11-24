"""Rates require a strictly positive value, including fractions below one."""
import pytest
from pydantic import ValidationError

from round_robin.research.calculator import Operand


def test_rate_rejects_zero():
    with pytest.raises(ValidationError):
        Operand(amount="0", unit="dimensionless", kind="rate")
    with pytest.raises(ValidationError):
        Operand(amount="0.000000", unit="dimensionless", kind="rate")


@pytest.mark.parametrize("value", ["0.000000000001", "0.5", "1"])
def test_rate_accepts_positive_values_at_and_below_one(value):
    assert Operand(amount=value, unit="dimensionless", kind="rate").amount == value
