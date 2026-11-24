from sqlalchemy import select

from round_robin.database.models import Event, Run, Step
from round_robin.providers.base import ProviderFailure
from round_robin.worker.tasks import _settle_failure
from backend.tests.test_orchestration import factory as factory, seed  # noqa: F401


def test_fallback_persists_reassignment_identity_and_reason(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, 'fallbacks': {**run.configuration['fallbacks'], 'intake': ['anthropic']}}
        step = Step(id='fallback_step', run_id=run_id, stage='intake', round=1,
                    role_slot_id='moderator', provider='openai', status='authorized',
                    calls=1, infrastructure_claims=1)
        session.add(step)
        session.flush()
        _settle_failure(session, run, step, ProviderFailure('authentication', retryable=False))
    with factory() as session:
        step = session.get(Step, 'fallback_step')
        assert step.status == 'pending' and step.provider == 'anthropic'
        events = list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == 'role.reassigned')))
        assert len(events) == 1
        assert events[0].payload == {'step_id': step.id, 'original_provider': 'openai',
                                     'provider': 'anthropic', 'reason': 'authentication'}
