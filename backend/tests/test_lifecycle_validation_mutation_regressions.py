from datetime import date, datetime, timezone

import pytest
from round_robin.fixtures import sample_brief
from round_robin.research import calculator
from round_robin.research.calculator import CalculationError, lifecycle_total


def test_omitted_quote_date_uses_utc_at_day_boundary(monkeypatch):
    class BoundaryClock:
        @classmethod
        def now(cls, tz=None):
            # At this instant, the local date is still the previous day.
            return datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc) if tz is timezone.utc else datetime(2026, 1, 1, 17, 30)

    monkeypatch.setattr(calculator, "datetime", BoundaryClock)
    option = sample_brief().options[0]
    component = option.cost_components[0].model_copy(update={"quote_valid_until": date(2026, 1, 1)})
    result = lifecycle_total(option.model_copy(update={"cost_components": [component]}), 12, "USD", calculation_id="calc_test")
    assert result.output is None
    assert result.known_subtotal == "1000.00"
    assert "expired" in result.missing_components[0].lower()


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"horizon_months": 0}, "horizon must be an integer from 1 to 120 months"),
        ({"as_of": datetime(2026, 1, 1)}, "quote freshness requires an explicit calendar date"),
        ({"currency": "ZZZ"}, "currency has no supported iso 4217 minor unit"),
    ],
)
def test_invalid_calculation_inputs_explain_the_problem(kwargs, message):
    arguments = {"horizon_months": 12, "currency": "USD", "calculation_id": "calc_test", "as_of": date(2026, 1, 1)}
    arguments.update(kwargs)
    with pytest.raises(CalculationError) as error:
        lifecycle_total(sample_brief().options[0], **arguments)
    assert str(error.value).casefold() == message
