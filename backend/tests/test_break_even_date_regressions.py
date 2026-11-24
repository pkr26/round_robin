"""Deterministic quote-date behavior for break-even calculations."""

from datetime import date, datetime, timezone

import pytest

from round_robin.fixtures import sample_brief
from round_robin.research import calculator
from round_robin.research.calculator import CalculationError, break_even
from round_robin.schemas.briefs import Option


def test_default_quote_date_uses_utc_even_when_local_date_is_previous_day(monkeypatch):
    first, second = sample_brief().options[:2]
    data = first.model_dump()
    data["cost_components"][0]["quote_valid_until"] = date(2026, 1, 1)
    first = Option.model_validate(data)
    requested_zones = []

    class Clock:
        @staticmethod
        def now(tz=None):
            requested_zones.append(tz)
            if tz == timezone.utc:
                return datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc)
            return datetime(2026, 1, 1, 17, 30)

    monkeypatch.setattr(calculator, "datetime", Clock)
    with pytest.raises(CalculationError, match="current quotes"):
        break_even(first, second, "USD")
    assert requested_zones == [timezone.utc]
    assert break_even(first, second, "USD", as_of=date(2026, 1, 1)).output == "20"
    assert requested_zones == [timezone.utc]


@pytest.mark.parametrize("invalid_date", [datetime(2026, 1, 1), "2026-01-01", 1])
def test_break_even_rejects_non_calendar_quote_dates(invalid_date):
    first, second = sample_brief().options[:2]
    with pytest.raises(CalculationError, match="calendar date"):
        break_even(first, second, "USD", as_of=invalid_date)
