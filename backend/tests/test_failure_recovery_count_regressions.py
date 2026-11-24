import pytest
from sqlalchemy import select

from round_robin.database.models import Event, Outbox, Run, Step
from round_robin.providers.base import ProviderFailure
from round_robin.worker.tasks import _settle_failure
from backend.tests.test_orchestration import factory as factory, seed  # noqa: F401


@pytest.mark.parametrize('prior_calls,prior_claims', [(1, 1), (0, 2)])
def test_recovery_counts_calls_and_claims_independently(factory, prior_calls, prior_claims):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        target = Step(id='failed_step', run_id=run_id, stage='intake', round=1,
                      role_slot_id='moderator', provider='openai', status='authorized',
                      calls=1, infrastructure_claims=1)
        # This constructed prior step isolates call and claim exposure separately.
        prior = Step(id='prior', run_id=run_id, stage='intake', round=1, stage_iteration=1,
                     role_slot_id='moderator', provider='openai', status='failed',
                     calls=prior_calls, infrastructure_claims=prior_claims)
        session.add_all([target, prior])
        session.flush()
        _settle_failure(session, run, target, ProviderFailure('timeout', retryable=True))
    with factory() as session:
        step = session.get(Step, 'failed_step')
        assert step.status == 'failed'
        assert step.error_code == 'timeout'
        assert session.scalar(select(Outbox).where(Outbox.run_id == run_id, Outbox.kind == 'execute', Outbox.step_id == step.id)) is None
        assert session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == 'step.retry_scheduled')) is None
