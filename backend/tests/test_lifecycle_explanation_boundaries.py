from datetime import date

import pytest

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import lifecycle_total
from round_robin.schemas.briefs import CostComponent, Option


@pytest.mark.parametrize("expired,label_limit,reason_limit", [(False, 240, 258), (True, 200, 220)])
def test_unknown_component_keeps_bounded_label_and_reason(expired, label_limit, reason_limit):
    option_data = sample_brief().options[0].model_dump()
    component_data = option_data["cost_components"][0]
    component_data.update(label="L" * 300, amount=None, unknown_reason="R" * 300,
                          quote_valid_until=date(2025, 12, 31) if expired else None)
    component = CostComponent.model_validate(component_data)
    option_data["cost_components"] = [component.model_dump()]
    option = Option.model_validate(option_data)
    result = lifecycle_total(option, 12, "USD", calculation_id="calc_test", as_of=date(2026, 1, 1))
    prefix = "Quote expired 2025-12-31; obtain a current quote. " if expired else ""
    assert result.output is None
    assert result.missing_components == [prefix + "L" * (label_limit - 1) + "…: " + "R" * (reason_limit - 1) + "…"]


def test_excluded_component_keeps_bounded_label_and_explanation():
    option_data = sample_brief().options[0].model_dump()
    component_data = option_data["cost_components"][0]
    component_data.update(label="L" * 400, status="excluded")
    component = CostComponent.model_validate(component_data)
    option_data["cost_components"] = [component.model_dump()]
    option = Option.model_validate(option_data)
    result = lifecycle_total(option, 12, "USD", calculation_id="calc_test", as_of=date(2026, 1, 1))
    assert result.output is None
    assert result.missing_components == ["L" * 379 + "…: excluded from the supplied cost; confirm the applicable charge or mark it not applicable."]
