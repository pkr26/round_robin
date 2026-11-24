from round_robin.research.calculator import Operand, Operation, evaluate


def test_maximum_supported_intermediate_is_inclusive():
    billion = Operand(amount='1000000000', unit='dimensionless', kind='quantity')
    thousand = Operand(amount='1000', unit='dimensionless', kind='quantity')
    value, unit, flags = evaluate(Operation(operation='multiply', operands=[billion, billion, billion, thousand]))
    assert value == '1000000000000000000000000000000'
    assert unit == 'dimensionless'
    assert flags == []
