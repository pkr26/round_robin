import pytest
from sqlalchemy import select

from round_robin.database.models import Event, Outbox, Run, Step
from round_robin.providers.base import ProviderFailure
from round_robin.worker.tasks import _settle_failure
from backend.tests.test_orchestration import factory as factory, seed  # noqa: F401


@pytest.mark.parametrize('difference', ['run', 'stage', 'round', 'role', 'none'])
def test_retry_counts_only_same_logical_step(factory, difference):
    run_id = seed(factory)
    foreign_id = seed(factory, suffix='foreign')
    with factory.begin() as session:
        run = session.get(Run, run_id)
        target = Step(id='failed_step', run_id=run_id, stage='intake', round=1,
                      role_slot_id='moderator', provider='openai', status='authorized',
                      calls=1, infrastructure_claims=1)
        prior = dict(id='prior', run_id=run_id, stage='intake', round=1, stage_iteration=1,
                     role_slot_id='moderator', provider='openai', status='failed',
                     calls=1, infrastructure_claims=2)
        if difference == 'run': prior['run_id'] = foreign_id
        if difference == 'stage': prior['stage'] = 'proposal'
        if difference == 'round': prior['round'] = 2
        if difference == 'role': prior['role_slot_id'] = 'other'
        # The competing rows deliberately construct histories to isolate each scope.
        session.add_all([target, Step(**prior)])
        session.flush()
        _settle_failure(session, run, target, ProviderFailure('timeout', retryable=True))
    with factory() as session:
        step = session.get(Step, 'failed_step')
        scheduled = session.scalar(select(Outbox).where(Outbox.run_id == run_id, Outbox.kind == 'execute', Outbox.step_id == step.id))
        event = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == 'step.retry_scheduled'))
        if difference == 'none':
            assert step.status == 'failed'
            assert scheduled is None and event is None
            failed = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == 'step.failed'))
            assert failed is not None
            expected = {'step_id': step.id, 'stage': 'intake',
                        'provider': 'openai', 'error_code': 'timeout'}
            assert {key: failed.payload.get(key) for key in expected} == expected
        else:
            assert step.status == 'pending'
            assert scheduled is not None and event is not None
            assert step.error_code == 'timeout'
