"""Pydantic money, quantity, and rate fields preserve their stated bounds."""
import pytest
from pydantic import TypeAdapter, ValidationError
from round_robin.schemas.common import Money, Quantity, Rate


@pytest.mark.parametrize(('schema', 'value'), [
    (Money, '0'), (Money, '1000000000000'), (Money, '0.123456'),
    (Quantity, '0'), (Quantity, '1000000000'), (Quantity, '0.123456'),
    (Rate, '0.000000000001'), (Rate, '1'), (Rate, '1000000'),
])
def test_numeric_field_accepts_valid_values(schema, value):
    assert TypeAdapter(schema).validate_python(value) == value


@pytest.mark.parametrize(('schema', 'value'), [
    (Money, '1000000000001'), (Money, '0.1234567'),
    (Quantity, '1000000001'), (Quantity, '0.1234567'),
    (Rate, '0'), (Rate, '1000001'), (Rate, '0.1234567890123'),
])
def test_numeric_field_rejects_invalid_values(schema, value):
    with pytest.raises(ValidationError):
        TypeAdapter(schema).validate_python(value)
