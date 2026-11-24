from decimal import Decimal

import pytest
from pydantic import ValidationError

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import CalculationError, Operand, Operation, break_even, evaluate, lifecycle_total
from round_robin.schemas.briefs import CostComponent


def total(option, months):
    return lifecycle_total(option, months, "USD", calculation_id="calc_test")


def test_preregistered_fictional_totals_and_break_even():
    first, second, baseline = sample_brief().options
    assert [total(option, 12).output for option in (first, second, baseline)] == ["1600.00", "1840.00", "0.00"]
    assert [total(option, 24).output for option in (first, second)] == ["2200.00", "2080.00"]
    assert total(first, 20).output == total(second, 20).output
    assert break_even(first, second, "USD").output == "20"


def test_missing_installation_is_not_zero():
    option = sample_brief(24, missing_installation=True).options[1]
    result = total(option, 24)
    assert result.output is None
    assert result.known_subtotal == "2080.00"
    assert "installation" in result.missing_components[0].lower()
    with pytest.raises(CalculationError, match="complete costs"):
        break_even(sample_brief().options[0], option, "USD")


def test_absent_or_excluded_cost_components_do_not_manufacture_zero():
    option = sample_brief().options[0]
    assert total(option.model_copy(update={"cost_components": []}), 12).output is None
    excluded = [component.model_copy(update={"status": "excluded"}) for component in option.cost_components]
    assert total(option.model_copy(update={"cost_components": excluded}), 12).output is None


def test_break_even_preserves_actual_component_provenance():
    first, second = sample_brief().options[:2]
    first = first.model_copy(update={"cost_components": [component.model_copy(update={"source_ref": "passage_custom_a"}) for component in first.cost_components]})
    result = break_even(first, second, "USD")
    assert "passage_custom_a" in {item.source_ref for item in result.inputs}
    assert "passage_offer_a" not in {item.source_ref for item in result.inputs}


def test_aggregate_total_can_exceed_per_component_bound_within_global_bound():
    option = sample_brief().options[0]
    component = option.cost_components[0].model_copy(update={"amount": "1000000000000", "quantity": "1000000000"})
    result = total(option.model_copy(update={"cost_components": [component]}), 120)
    assert result.output == "1000000000000000000000.00"


@pytest.mark.parametrize("value", [0, 1.2, True, "NaN", "Infinity", "-1", "1e3", "01", "1.1234567", "1000000000001", "1234567890123.123456", "1" * 33])
def test_costs_reject_nondecimal_or_out_of_bounds_values(value):
    data = sample_brief().options[0].cost_components[0].model_dump()
    data["amount"] = value
    with pytest.raises(ValidationError):
        CostComponent.model_validate(data)


@pytest.mark.parametrize("horizon", [0, 121, True, 2.5])
def test_horizon_bounds(horizon):
    with pytest.raises(CalculationError):
        total(sample_brief().options[0], horizon)


def test_explicit_zero_and_unknown_reason_are_distinct():
    zero = sample_brief().options[0].cost_components[2]
    assert zero.amount == "0"
    data = zero.model_dump()
    data["amount"] = None
    with pytest.raises(ValidationError, match="explicit reason"):
        CostComponent.model_validate(data)


def test_mixed_currencies_need_a_dated_conversion_basis():
    data = sample_brief().options[0].model_dump()
    data["cost_components"][0]["currency"] = "EUR"
    option = type(sample_brief().options[0]).model_validate(data)
    with pytest.raises(CalculationError, match="Currency conversion"):
        total(option, 12)


def test_discount_is_explicit_and_cannot_make_total_negative():
    option = sample_brief().options[0]
    discount = option.cost_components[0].model_copy(update={"id": "discount_test", "kind": "discount", "amount": "100"})
    option = option.model_copy(update={"cost_components": [*option.cost_components, discount]})
    assert total(option, 12).output == "1500.00"
    option = option.model_copy(update={"cost_components": [discount]})
    with pytest.raises(CalculationError, match="negative"):
        total(option, 12)


def test_exact_decimal_arithmetic_does_not_use_binary_floats():
    operation = Operation(operation="add", operands=[Operand(amount="0.1", unit="USD", kind="money"), Operand(amount="0.2", unit="USD", kind="money")])
    assert evaluate(operation) == ("0.3", "USD", [])


def test_division_retains_rounding_flags_and_zero_is_rejected():
    operation = Operation(operation="divide", operands=[Operand(amount="1", unit="dimensionless", kind="quantity"), Operand(amount="3", unit="dimensionless", kind="quantity")])
    value, unit, flags = evaluate(operation)
    assert Decimal(value) < 1
    assert unit == "dimensionless"
    assert flags == ["division rounded at Decimal precision 64"]
    operation.operands[1] = Operand(amount="0", unit="dimensionless", kind="quantity")
    with pytest.raises(CalculationError, match="zero"):
        evaluate(operation)


def test_dimension_mismatch_and_arbitrary_operations_are_rejected():
    with pytest.raises(CalculationError, match="compatible"):
        evaluate(Operation(operation="add", operands=[Operand(amount="1", unit="USD", kind="money"), Operand(amount="1", unit="EUR", kind="money")]))
    with pytest.raises(ValidationError):
        Operation.model_validate({"operation": "eval", "operands": [], "code": "print('not executed')"})


def test_global_operand_count_and_operation_depth_are_bounded():
    operand = Operand(amount="1", unit="dimensionless", kind="quantity")
    node = Operation(operation="add", operands=[operand, operand])
    for _ in range(4):
        node = Operation(operation="add", operands=[node, operand])
    with pytest.raises(CalculationError, match="four"):
        evaluate(node)
    inner = Operation(operation="add", operands=[operand] * 120)
    with pytest.raises(CalculationError, match="120"):
        evaluate(Operation(operation="add", operands=[inner, operand]))


def test_intermediate_magnitude_is_bounded():
    operand = Operand(amount="1000000000", unit="dimensionless", kind="quantity")
    with pytest.raises(CalculationError, match=r"10\^30"):
        evaluate(Operation(operation="multiply", operands=[operand] * 4))
