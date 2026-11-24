from types import SimpleNamespace
import pytest
from round_robin.orchestration.budget import BudgetDenied, checked_finish_hold


class Session:
    def __init__(self, hold): self.hold = hold
    def scalar(self, query): return self.hold


def run(status='running'):
    return SimpleNamespace(id='run_1', owner_id='owner_1', status=status,
                           configuration={'mode': 'live', 'finish_policy': {'reserved_usd': '0.40'}})


def hold(**changes):
    values = {'run_id': 'run_1', 'owner_id': 'owner_1', 'kind': 'finish_hold', 'status': 'held',
              'reserved_usd': '0.20', 'settled_usd': None,
              'metadata_json': {'roles_remaining': ['audit'], 'initial_reserved_usd': '0.40'}}
    values.update(changes)
    return SimpleNamespace(**values)


def test_absent_and_reserve_boundary():
    assert checked_finish_hold(Session(None), run()) is None
    row = hold()
    assert checked_finish_hold(Session(row), run()) is row
    with pytest.raises(BudgetDenied, match='^finish_money_reserve$'):
        checked_finish_hold(Session(hold(reserved_usd='0.19')), run())


@pytest.mark.parametrize('changes', [
    {'run_id': 'other'}, {'owner_id': 'other'}, {'kind': 'model'}, {'status': 'released'},
    {'settled_usd': '0.01'}, {'metadata_json': {'roles_remaining': ['audit'], 'initial_reserved_usd': '0.40', 'conservative_booking_usd': '0.1'}},
    {'metadata_json': {'roles_remaining': ['audit', 'audit'], 'initial_reserved_usd': '0.40'}},
    {'metadata_json': {'roles_remaining': ['foreign'], 'initial_reserved_usd': '0.40'}},
    {'metadata_json': {'roles_remaining': ['audit'], 'initial_reserved_usd': '0.40', 'extra': True}},
    {'metadata_json': {'roles_remaining': ['audit']}},
    {'metadata_json': {'roles_remaining': ['audit'], 'initial_reserved_usd': '0.30'}},
    {'metadata_json': {'roles_remaining': ['audit'], 'initial_reserved_usd': '0.40', 'correction_reserved_usd': '0.20'}},
])
def test_invalid_active_hold_is_denied(changes):
    with pytest.raises(BudgetDenied, match='^ledger_integrity_failed$'):
        checked_finish_hold(Session(hold(**changes)), run())


def test_awaiting_input_requires_active_hold():
    with pytest.raises(BudgetDenied, match='^ledger_integrity_failed$'):
        checked_finish_hold(Session(hold(status='released', reserved_usd='0')), run('awaiting_input'))


def test_release_and_terminal_allow_only_zero_released_balance():
    released = hold(status='released', reserved_usd='0')
    assert checked_finish_hold(Session(released), run(), for_release=True) is released
    assert checked_finish_hold(Session(released), run('failed')) is released
    with pytest.raises(BudgetDenied, match='^ledger_integrity_failed$'):
        checked_finish_hold(Session(hold(status='released', reserved_usd='0.01')), run('failed'))


def test_matching_correction_reserve_is_accepted():
    metadata = {'roles_remaining': ['audit'], 'initial_reserved_usd': '0.40', 'correction_reserved_usd': '0.40'}
    row = hold(metadata_json=metadata)
    assert checked_finish_hold(Session(row), run()) is row


@pytest.mark.parametrize('metadata', [{'roles_remaining': ['audit']}, {'initial_reserved_usd': '0.40'}])
def test_terminal_partial_hold_metadata_is_denied(metadata):
    with pytest.raises(BudgetDenied, match='^ledger_integrity_failed$'):
        checked_finish_hold(Session(hold(metadata_json=metadata)), run('failed'))
