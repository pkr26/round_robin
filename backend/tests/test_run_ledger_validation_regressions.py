from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from round_robin.orchestration.budget import BudgetDenied, validate_run_ledger


def row(id='op_1', *, status='settled', kind='model', reserved='0.2', settled='0', metadata=None):
    return SimpleNamespace(id=id, status=status, kind=kind, reserved_usd=reserved, settled_usd=settled,
                           authorized_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                           metadata_json={'model_call': True} if metadata is None else metadata)


def test_valid_calls_and_simulation():
    assert validate_run_ledger([row(), row('op_2', kind='finish_hold', status='held', settled=None, metadata={})], simulated=True) == 1
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        validate_run_ledger([row(settled='0.2')], simulated=True)
    assert validate_run_ledger([row(settled='0.2')], simulated=False) == 1


def test_uncertainty_keeps_operations_unfinalized():
    assert validate_run_ledger([row(status='reserved', settled='0.2')], simulated=True) == 1
    assert validate_run_ledger([row(status='unknown', settled='0.2')], simulated=True) == 1
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        validate_run_ledger([row('duplicate', status='unknown'), row('duplicate', status='unknown')], simulated=False)


def test_too_many_model_calls_or_unknowns_are_denied():
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        validate_run_ledger([row(f'op_{i}') for i in range(25)], simulated=False)
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        validate_run_ledger([row(f'op_{i}', status='unknown', kind='tool', metadata={}) for i in range(65)], simulated=False)


def test_released_amount_still_requires_bounded_operand():
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        validate_run_ledger([row(status='released', settled='0e31')], simulated=False)


def test_invalid_settled_held_and_unknown_operands_are_denied():
    for bad in [row(settled='NaN'), row(status='held', kind='finish_hold', reserved='-1', settled=None, metadata={}),
                row(status='unknown', settled='0', metadata={'model_call': True, 'conservative_booking_usd': 'NaN'})]:
        with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
            validate_run_ledger([bad], simulated=False)

