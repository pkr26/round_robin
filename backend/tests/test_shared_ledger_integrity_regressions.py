from uuid import uuid4

import pytest

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start
from round_robin.database.models import BudgetOperation, Command, Run
from round_robin.orchestration.budget import BudgetDenied, model_call_count, operations_exposure
from round_robin.orchestration.controller import finalize_run
from round_robin.schemas.views import RunView


@pytest.mark.parametrize('bad_metadata', [None, [], 'hidden', {'model_call': 'true'}, {'model_call': None}, {'model_call': 1}])
def test_cancel_commits_then_finalizes_without_erasing_unknown_exposure(application, client, bad_metadata):
    meeting = create(client)
    response = start(client, meeting)
    assert response.status_code == 202
    run = response.json()
    with application.state.session_factory.begin() as session:
        session.add(BudgetOperation(id='ledger_bad', run_id=run['id'], owner_id='owner_local',
            kind='model', status='unknown', reserved_usd='0.40', metadata_json=bad_metadata))
        session.add(BudgetOperation(id=f"finish_{run['id']}", run_id=run['id'], owner_id='owner_local',
            kind='finish_hold', status='held', reserved_usd='0.20', metadata_json={'roles_remaining': ['audit']}))
    payload = {'command_id': str(uuid4()), 'expected_run_version': run['version']}
    result = client.post(f"/api/runs/{run['id']}/cancel", json=payload)
    assert result.status_code == 200
    body = result.json()
    RunView.model_validate(body)
    assert body['status'] == 'cancel_requested'
    assert body['remaining_limits']['requests'] is None
    assert body['remaining_limits']['requests_unavailable_reason'] == 'ledger_integrity_failed'
    assert client.post(f"/api/runs/{run['id']}/cancel", json=payload).json() == body
    with application.state.session_factory.begin() as session:
        saved = session.get(Run, run['id'])
        assert saved.execution_fence == 1 and session.get(Command, ('owner_local', payload['command_id']))
        finalize_run(session, saved)
        assert saved.status == 'cancelled' and saved.stop_reason == 'user_cancellation'
        assert saved.configuration['ledger_integrity_failed'] is True
        assert saved.report_version == 0 and saved.execution_fence == 2
        assert session.get(BudgetOperation, 'ledger_bad').reserved_usd == '0.40'
        assert session.get(BudgetOperation, f"finish_{run['id']}").reserved_usd == '0.20'
        version = saved.version
        finalize_run(session, saved)
        assert saved.version == version
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()['report'] is None and snapshot.json()['run']['readiness'] is None
    assert snapshot.json()['retry_eligible'] is False
    assert client.get(f"/api/runs/{run['id']}/report").status_code == 503


def test_money_exposure_checks_only_active_metadata():
    def row(status, metadata, settled=None):
        return BudgetOperation(status=status, reserved_usd='0.40', settled_usd=settled, metadata_json=metadata)
    assert str(operations_exposure([row('released', [])])) == '0'
    from round_robin.database.models import utcnow
    settled = row('settled', [], '0.17')
    settled.authorized_at = utcnow()
    assert str(operations_exposure([settled])) == '0.17'
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        operations_exposure([row('unknown', [])])
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        model_call_count([settled])


@pytest.mark.parametrize('status,reserved,settled,metadata', [
    ('unknown', '0.40', None, {'model_call': True, 'conservative_booking_usd': 'NaN'}),
    ('reserved', 'NaN', None, {'model_call': True}),
    ('held', '-1', None, {}),
    ('settled', '0.40', None, {'model_call': True}),
])
def test_invalid_money_keeps_finish_hold_and_returns_unavailable(application, client, status, reserved, settled, metadata):
    meeting = create(client)
    run = start(client, meeting).json()
    with application.state.session_factory.begin() as session:
        session.add(BudgetOperation(id='bad_amount', run_id=run['id'], owner_id='owner_local', kind='model',
            status=status, reserved_usd=reserved, settled_usd=settled, metadata_json=metadata))
        session.add(BudgetOperation(id=f"finish_{run['id']}", run_id=run['id'], owner_id='owner_local',
            kind='finish_hold', status='held', reserved_usd='0.20', metadata_json={'roles_remaining': ['audit']}))
    stop = client.post(f"/api/runs/{run['id']}/cancel", json={'command_id': str(uuid4()), 'expected_run_version': run['version']})
    assert stop.status_code == 200 and stop.json()['remaining_limits']['requests'] is None
    with application.state.session_factory.begin() as session:
        saved = session.get(Run, run['id'])
        finalize_run(session, saved)
        assert saved.status == 'cancelled' and saved.configuration['ledger_integrity_failed'] is True
        assert session.get(BudgetOperation, f"finish_{run['id']}").reserved_usd == '0.20'
        bad = session.get(BudgetOperation, 'bad_amount')
        assert (bad.status, bad.reserved_usd, bad.settled_usd, bad.metadata_json) == (status, reserved, settled, metadata)


@pytest.mark.parametrize('bad', ['1e99999999', '1e-99999999', '9' * 100000, '0.' + '0' * 100000 + '1'])
def test_extreme_stored_liability_is_rejected_without_formatting_unbounded_numbers(bad):
    row = BudgetOperation(id='bad_extreme', run_id='run_test', owner_id='owner_test', kind='model',
        status='unknown', reserved_usd=bad, metadata_json={'model_call': True})
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        operations_exposure([row])


def test_saved_packet_digest_is_canonical_and_type_sensitive_and_rejects_non_json():
    from round_robin.worker.tasks import _saved_packet_digest

    assert _saved_packet_digest({'b': [1, False], 'a': 'public'}) == _saved_packet_digest({'a': 'public', 'b': [1, False]})
    assert _saved_packet_digest({'repair': False}) != _saved_packet_digest({'repair': 0})
    assert _saved_packet_digest({'temperature': float('nan')}) is None
    assert _saved_packet_digest({'unsupported': object()}) is None
