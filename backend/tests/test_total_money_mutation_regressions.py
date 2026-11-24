from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from round_robin.schemas.common import TotalMoney, total_money


@pytest.mark.parametrize('value', ['0', '0.000001', '123.456789', '1000000000000000000000000000000'])
def test_valid_calculated_amounts(value):
    assert TypeAdapter(TotalMoney).validate_python(value) == value
    assert total_money(value) == value


@pytest.mark.parametrize('value', [None, 1, Decimal('1'), '-1', '1e3', '01', '1.', '.1', '1.0000000', '1000000000000000000000000000001', '9' * 64])
def test_invalid_calculated_amounts(value):
    with pytest.raises(ValidationError):
        TypeAdapter(TotalMoney).validate_python(value)
    with pytest.raises(ValueError):
        total_money(value)
