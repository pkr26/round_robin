"""Ledger exposure counts the correct period and the highest uncertain liability."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from round_robin.orchestration.budget import BudgetDenied, operations_exposure


def operation(status, *, reserved='0', settled=None, booking=None, at=None, metadata=None):
    return SimpleNamespace(status=status, reserved_usd=reserved, settled_usd=settled,
                           authorized_at=at or datetime(2026, 1, 1, tzinfo=timezone.utc),
                           metadata_json=({'conservative_booking_usd': booking} if booking is not None else {}) if metadata is None else metadata)


def test_monthly_settlements_require_same_year_and_month():
    now = datetime(2026, 2, 10, tzinfo=timezone.utc)
    rows = [operation('settled', settled='1', at=datetime(year, 2, 5, tzinfo=timezone.utc))
            for year in (2025, 2026, 2027)]
    assert operations_exposure(rows, now=now, period='monthly') == Decimal('1')


@pytest.mark.parametrize(('reserved', 'settled', 'booking'), [
    ('0.9', '0.1', None), ('0.1', '0.9', None), ('0.1', '0.2', '0.9'),
])
def test_uncertain_exposure_is_highest_known_amount_once(reserved, settled, booking):
    row = operation('unknown', reserved=reserved, settled=settled, booking=booking,
                    at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    for period in (None, 'daily', 'monthly'):
        assert operations_exposure([row], now=datetime(2026, 2, 1, tzinfo=timezone.utc), period=period) == Decimal('0.9')


def test_unsettled_missing_amount_and_invalid_metadata():
    assert operations_exposure([operation('reserved', reserved='0.4')]) == Decimal('0.4')
    row = operation('unknown', reserved='0.4'); row.metadata_json = None
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        operations_exposure([row])


def test_daily_settlement_uses_utc_date():
    local = timezone(timedelta(hours=5, minutes=30))
    row = operation('settled', settled='1', at=datetime(2026, 1, 1, 0, 30, tzinfo=local))
    now = datetime(2025, 12, 31, 20, 0, tzinfo=timezone.utc)
    assert operations_exposure([row], now=now, period='daily') == Decimal('1')
