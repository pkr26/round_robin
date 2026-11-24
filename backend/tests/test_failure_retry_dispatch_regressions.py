from datetime import timedelta

import pytest
from sqlalchemy import select

from round_robin.database.models import Event, Outbox, Run, Step, utcnow
from round_robin.providers.base import ProviderFailure
from round_robin.worker import tasks
from backend.tests.test_orchestration import factory as factory, seed  # noqa: F401


@pytest.mark.parametrize('state', ['absent', 'existing'])
def test_retry_dispatch_preserves_generation_and_schedule(factory, monkeypatch, state):
    run_id = seed(factory)
    now = utcnow()
    monkeypatch.setattr(tasks, 'utcnow', lambda: now)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        if state == 'existing':
            run.configuration = {**run.configuration, 'step_dispatch': {
                'failed_step': {'generation': 7, 'not_before': now.isoformat()},
                'other': {'generation': 4},
            }}
        step = Step(id='failed_step', run_id=run_id, stage='intake', round=1,
                    role_slot_id='moderator', provider='openai', status='authorized',
                    calls=1, infrastructure_claims=1, lease_owner='previous',
                    lease_expires_at=now - timedelta(seconds=1))
        session.add(step)
        session.flush()
        tasks._settle_failure(session, run, step, ProviderFailure('timeout', retryable=True, retry_after=11))
    with factory() as session:
        run = session.get(Run, run_id)
        step = session.get(Step, 'failed_step')
        controls = run.configuration['step_dispatch']
        expected_due = now + timedelta(seconds=11)
        assert step.status == 'pending'
        assert step.lease_owner is None and step.lease_expires_at is None
        assert controls['failed_step']['generation'] == (8 if state == 'existing' else 1)
        assert controls['failed_step']['not_before'] == expected_due.isoformat()
        if state == 'existing':
            assert controls['other'] == {'generation': 4}
        outbox = session.scalar(select(Outbox).where(Outbox.run_id == run_id, Outbox.kind == 'execute', Outbox.step_id == step.id))
        assert outbox is not None
        assert outbox.due_at.replace(tzinfo=expected_due.tzinfo) == expected_due
        event = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == 'step.retry_scheduled'))
        assert event is not None
        expected_payload = {'step_id': step.id, 'error_code': 'timeout',
                            'calls_used': 1, 'retry_after_seconds': 11}
        assert {key: event.payload.get(key) for key in expected_payload} == expected_payload
