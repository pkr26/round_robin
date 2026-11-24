from datetime import date

import pytest
from round_robin.fixtures import sample_brief
from round_robin.research.calculator import lifecycle_total


def test_month_one_is_a_valid_horizon_and_metadata_stays_attached():
    result = lifecycle_total(sample_brief().options[0], 1, "USD", calculation_id="calc_test", as_of=date(2026, 1, 1))
    assert result.output == "1050.00"
    assert result.horizon_months == 1 and result.currency == "USD"


@pytest.mark.parametrize("status", ["excluded", "not_applicable"])
def test_skipped_components_do_not_hide_later_entered_amounts(status):
    option = sample_brief().options[0]
    first = option.cost_components[0].model_copy(update={"status": status})
    monthly = option.cost_components[1]
    value = lifecycle_total(option.model_copy(update={"cost_components": [first, monthly]}), 12, "USD", calculation_id="calc_test")
    assert value.known_subtotal == "600.00"
    assert [item.label for item in value.inputs] == [monthly.label]
    if status == "excluded":
        assert value.output is None
        assert len(value.missing_components) == 1
        assert first.label in value.missing_components[0]
    else:
        assert value.output == "600.00"


def test_formula_explains_monthly_and_discount_arithmetic():
    option = sample_brief().options[0]
    first, monthly = option.cost_components[:2]
    discount = first.model_copy(update={"id": "discount_test", "kind": "discount", "amount": "100"})
    value = lifecycle_total(option.model_copy(update={"cost_components": [first, monthly, discount]}), 12, "USD", calculation_id="calc_test")
    assert value.output == "1500.00"
    assert value.formula.startswith("1000 × 1")
    assert "50 × 1 × 12 months" in value.formula
    assert "− 100 × 1" in value.formula
    assert "100 × 1 × 12 months" not in value.formula


def test_excluded_only_cost_has_an_excluded_caveat_without_saying_no_components():
    option = sample_brief().options[0]
    component = option.cost_components[0].model_copy(update={"status": "excluded"})
    value = lifecycle_total(option.model_copy(update={"cost_components": [component]}), 12, "USD", calculation_id="calc_test")
    assert value.output is None
    assert len(value.missing_components) == 1
    assert component.label in value.missing_components[0]


@pytest.mark.parametrize("amount,expected,flags", [("1.005", "1.00", ["final currency rounding"]), ("1.015", "1.02", ["final currency rounding"]), ("1.00", "1.00", [])])
def test_final_half_even_rounding_and_flags(amount, expected, flags):
    option = sample_brief().options[0]
    component = option.cost_components[0].model_copy(update={"amount": amount})
    value = lifecycle_total(option.model_copy(update={"cost_components": [component]}), 12, "USD", calculation_id="calc_test")
    assert value.output == expected and value.known_subtotal == expected
    assert value.currency == "USD" and value.horizon_months == 12
    assert value.flags == flags
