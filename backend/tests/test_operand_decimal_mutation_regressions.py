"""Calculator operands enforce plain decimal syntax and fractional limits."""
import pytest
from pydantic import ValidationError
from round_robin.research.calculator import Operand


@pytest.mark.parametrize('value', ['01', '1e3', '+1', ' 1'])
def test_operand_rejects_nonplain_decimal_strings(value):
    with pytest.raises(ValidationError):
        Operand(amount=value, unit='USD', kind='money')


@pytest.mark.parametrize(('kind', 'valid', 'invalid'), [
    ('money', '1.123456', '1.1234567'),
    ('quantity', '1.123456', '1.1234567'),
    ('rate', '1.123456789012', '1.1234567890123'),
])
def test_operand_enforces_kind_fractional_limit(kind, valid, invalid):
    assert Operand(amount=valid, unit='dimensionless', kind=kind).amount == valid
    with pytest.raises(ValidationError):
        Operand(amount=invalid, unit='dimensionless', kind=kind)
