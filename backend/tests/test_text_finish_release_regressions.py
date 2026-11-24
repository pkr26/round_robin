from uuid import uuid4

import pytest

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start
from round_robin.database.models import BudgetOperation, Run
from round_robin.orchestration.controller import finalize_run


@pytest.mark.parametrize('damage', ['foreign_owner', 'foreign_run', 'model_call'])
def test_cancel_does_not_release_unrelated_or_ambiguous_finish_hold(application, client, damage):
    run = start(client, create(client)).json()
    hold_id = f"finish_{run['id']}"
    with application.state.session_factory.begin() as session:
        session.add(BudgetOperation(id=hold_id, run_id='foreign_run' if damage == 'foreign_run' else run['id'],
            owner_id='foreign_owner' if damage == 'foreign_owner' else 'owner_local', kind='finish_hold', status='held',
            reserved_usd='0.40', metadata_json={'roles_remaining': ['audit'], **({'model_call': True} if damage == 'model_call' else {})}))
    response = client.post(f"/api/runs/{run['id']}/cancel", json={'command_id': str(uuid4()), 'expected_run_version': run['version']})
    assert response.status_code == 200
    with application.state.session_factory.begin() as session:
        finalize_run(session, session.get(Run, run['id']))
    with application.state.session_factory() as session:
        row = session.get(BudgetOperation, hold_id)
        assert row.status == 'held' and row.reserved_usd == '0.40'
        assert session.get(Run, run['id']).status == 'cancelled'


@pytest.mark.parametrize('metadata', [{'initial_reserved_usd': '0.40'}, {'roles_remaining': ['audit']}])
def test_partial_legacy_hold_metadata_remains_held_without_false_release(application, client, metadata):
    run = start(client, create(client)).json()
    identity = f"finish_{run['id']}"
    with application.state.session_factory.begin() as session:
        session.add(BudgetOperation(id=identity, run_id=run['id'], owner_id='owner_local', kind='finish_hold',
            status='held', reserved_usd='0.40', metadata_json=metadata))
    response = client.post(f"/api/runs/{run['id']}/cancel", json={'command_id': str(uuid4()), 'expected_run_version': run['version']})
    assert response.status_code == 200
    assert response.json()['remaining_limits']['requests'] is None
    assert response.json()['remaining_limits']['requests_unavailable_reason'] == 'ledger_integrity_failed'
    with application.state.session_factory.begin() as session:
        finalize_run(session, session.get(Run, run['id']))
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot")
    assert snapshot.status_code == 200 and snapshot.json()['report'] is None
    assert snapshot.json()['run']['remaining_limits']['requests'] is None
    with application.state.session_factory() as session:
        hold = session.get(BudgetOperation, identity)
        assert hold.status == 'held' and hold.reserved_usd == '0.40' and hold.metadata_json == metadata
        assert session.get(Run, run['id']).status == 'cancelled'
