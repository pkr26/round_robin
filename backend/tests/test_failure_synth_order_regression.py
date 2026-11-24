from sqlalchemy import select

from round_robin.database.models import Event, Outbox, Run, Step
from round_robin.providers.base import ProviderFailure
from round_robin.worker.tasks import _settle_failure
from backend.tests.test_orchestration import factory as factory, seed  # noqa: F401


def test_audit_fallback_avoids_latest_successful_synthesizer(factory):
    run_id = seed(factory)
    foreign_id = seed(factory, suffix="foreign")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, 'fallbacks': {**run.configuration['fallbacks'], 'audit': ['google', 'anthropic']}}
        # Competing rows isolate the lookup; this constructed history is synthetic.
        session.add_all([
            Step(id='old_synthesis', run_id=run_id, stage='synthesis', stage_iteration=0, role_slot_id='synthesis', provider='anthropic', status='succeeded'),
            Step(id='new_synthesis', run_id=run_id, stage='synthesis', stage_iteration=1, role_slot_id='synthesis', provider='google', status='succeeded'),
            Step(id='foreign_synthesis', run_id=foreign_id, stage='synthesis', stage_iteration=2, role_slot_id='synthesis', provider='anthropic', status='succeeded'),
            Step(id='failed_synthesis', run_id=run_id, stage='synthesis', stage_iteration=2, role_slot_id='synthesis', provider='anthropic', status='failed'),
            Step(id='other_success', run_id=run_id, stage='proposal', stage_iteration=2, role_slot_id='proposal', provider='anthropic', status='succeeded'),
            Step(id='audit_failure', run_id=run_id, stage='audit', stage_iteration=1, role_slot_id='audit', provider='openai', status='authorized', calls=1, infrastructure_claims=1),
        ])
        session.flush()
        step = session.get(Step, 'audit_failure')
        _settle_failure(session, run, step, ProviderFailure('authentication', retryable=False))
    with factory() as session:
        step = session.get(Step, 'audit_failure')
        assert step.status == 'pending'
        assert step.provider == 'anthropic'
        assert step.error_code == 'authentication'
        reassignment = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == 'role.reassigned'))
        assert reassignment.payload['provider'] == 'anthropic'
        assert session.scalar(select(Outbox).where(Outbox.run_id == run_id, Outbox.kind == 'execute', Outbox.step_id == step.id)) is not None
