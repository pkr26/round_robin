from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start
from round_robin.database.models import BudgetOperation, Event, Run
from round_robin.dispatcher.service import reconcile_once


@pytest.mark.parametrize('damage', ['foreign_owner', 'model_call'])
def test_dispatcher_does_not_claim_skipped_ambiguous_release(application, client, damage):
    run = start(client, create(client)).json()
    with application.state.session_factory.begin() as session:
        saved = session.get(Run, run['id'])
        saved.status, saved.stage, saved.stop_reason = 'failed', 'finished', 'worker_lost'
        session.add(BudgetOperation(id=f"finish_{run['id']}", run_id=run['id'],
            owner_id='foreign_owner' if damage == 'foreign_owner' else 'owner_local',
            kind='finish_hold', status='held', reserved_usd='0.40',
            metadata_json={} if damage == 'foreign_owner' else {'model_call': True}))
    reconcile_once(session_factory=application.state.session_factory)
    with application.state.session_factory() as session:
        hold = session.get(BudgetOperation, f"finish_{run['id']}")
        assert hold.status == 'held' and hold.reserved_usd == '0.40'
        assert not list(session.scalars(select(Event).where(Event.run_id == run['id'], Event.event_type == 'run.retry_reserve_released')))


def test_terminal_cancel_does_not_claim_release_when_locked_recheck_rejects(application, client, monkeypatch):
    run = start(client, create(client)).json()
    with application.state.session_factory.begin() as session:
        saved = session.get(Run, run['id'])
        saved.status, saved.stage, saved.stop_reason = 'failed', 'finished', 'worker_lost'
        session.add(BudgetOperation(id=f"finish_{run['id']}", run_id=run['id'], owner_id='owner_local',
            kind='finish_hold', status='held', reserved_usd='0.40', metadata_json={}))
    monkeypatch.setattr('round_robin.orchestration.budget.release_unused_finish', lambda session, saved: False)
    result = client.post(f"/api/runs/{run['id']}/cancel", json={'command_id': str(uuid4()), 'expected_run_version': run['version']})
    assert result.status_code == 200
    with application.state.session_factory() as session:
        hold = session.get(BudgetOperation, f"finish_{run['id']}")
        assert hold.status == 'held' and hold.reserved_usd == '0.40'
        assert not list(session.scalars(select(Event).where(Event.run_id == run['id'], Event.event_type == 'run.retry_abandoned')))
