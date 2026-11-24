import pytest
from round_robin.research.calculator import (
    CalculationError,
    Operand,
    Operation,
    evaluate,
)


def operand(value='1', unit='dimensionless'):
    return Operand(amount=value, unit=unit, kind='quantity')


def operation(kind, *operands):
    return Operation(operation=kind, operands=list(operands))


def test_operand_limit_accepts_exactly_120_across_tree():
    leaves = [operand()] * 120
    assert evaluate(operation('add', operation('add', *leaves[:60]), operation('add', *leaves[60:]))) == ('120', 'dimensionless', [])
    with pytest.raises(CalculationError, match='120'):
        evaluate(operation('add', operation('add', *leaves[:60]), operation('add', *leaves[60:]), operand()))


def test_four_levels_allowed_fifth_rejected():
    node = operation('add', operand(), operand())
    for _ in range(3):
        node = operation('add', node, operand())
    assert evaluate(node) == ('5', 'dimensionless', [])
    with pytest.raises(CalculationError, match='four'):
        evaluate(operation('add', node, operand()))


def test_subtract_keeps_operation_order():
    assert evaluate(operation('subtract', operand('10', 'USD'), operand('3', 'USD'), operand('2', 'USD'))) == ('5', 'USD', [])


def test_scale_multiplication_keeps_dimensional_unit_on_either_side():
    assert evaluate(operation('multiply', operand('3', 'USD'), operand('2'))) == ('6', 'USD', [])
    assert evaluate(operation('multiply', operand('2'), operand('3', 'USD'))) == ('6', 'USD', [])
    with pytest.raises(CalculationError, match='dimensionless'):
        evaluate(operation('multiply', operand('3', 'USD'), operand('2', 'items')))


def test_repeating_division_uses_64_digit_precision():
    value, unit, flags = evaluate(operation('divide', operand('1'), operand('3')))
    assert value == '0.' + '3' * 64
    assert unit == 'dimensionless'
    assert flags == ['division rounded at Decimal precision 64']


def test_serialized_length_boundary():
    parts = [operand('0.000001')] * 15 + [operand('0.0001')]
    value, unit, flags = evaluate(operation('multiply', *parts))
    assert value == '0.' + '0' * 93 + '1'
    assert len(value) == 96
    assert unit == 'dimensionless' and flags == []
    with pytest.raises(CalculationError, match='96'):
        evaluate(operation('multiply', *parts, operand('0.1')))
