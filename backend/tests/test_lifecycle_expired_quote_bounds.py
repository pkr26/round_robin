from datetime import date

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import lifecycle_total
from round_robin.schemas.briefs import CostComponent, Option


def test_expired_known_cost_keeps_the_expiry_message_with_a_long_label():
    option_data = sample_brief().options[0].model_dump()
    component_data = option_data["cost_components"][0]
    component_data.update(label="L" * 500, quote_valid_until=date(2025, 12, 31))
    component = CostComponent.model_validate(component_data)
    option_data["cost_components"] = [component.model_dump()]
    option = Option.model_validate(option_data)

    result = lifecycle_total(option, 12, "USD", calculation_id="calc_bounds", as_of=date(2026, 1, 1))

    assert result.output is None
    assert result.known_subtotal == "1000.00"
    assert result.missing_components == [
        "L" * 379 + "…: quote expired 2025-12-31; obtain a current quote for this comparison."
    ]
    assert len(result.missing_components[0]) <= 500
