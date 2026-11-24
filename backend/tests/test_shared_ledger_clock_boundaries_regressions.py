import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Attempt, BudgetOperation, Event, Run, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration import controller
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker import tasks


@pytest.mark.parametrize('boundary', ['eligibility', 'remaining', 'content', 'advance'])
def test_clock_regression_after_success_preserves_receipt_and_fences_retry(factory, monkeypatch, boundary):
    run_id = seed(factory)
    with factory.begin() as session:
        controller.advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == 'intake'))
    seen = {'sent': False, 'jumped': False}
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            seen['sent'] = True
            return result
    module, name = {'eligibility': (tasks, 'check_eligible'), 'remaining': (tasks, 'remaining_active'),
                    'content': (tasks, 'active_elapsed'), 'advance': (controller, 'check_eligible')}[boundary]
    original = getattr(module, name)
    def jump(*args, **kwargs):
        if seen['sent'] and not seen['jumped']:
            seen['jumped'] = True
            raise BudgetDenied('clock_regression')
        return original(*args, **kwargs)
    monkeypatch.setattr(module, name, jump)
    provider = Provider('openai')
    result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
    assert result['status'] == 'persisted' and provider.calls == 1 and seen['jumped']
    with factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert attempt.completed_at is not None and attempt.usage_certainty == 'reported'
        assert ledger.status == 'settled' and ledger.settled_usd == '0'
        assert run.configuration.get('active_clock_invalid') is True and run.active_started_at is None
        assert run.status in {'failed', 'completed_with_limitations'} and run.stop_reason == 'clock_regression'
        assert not list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type.in_(['step.retry_scheduled', 'role.reassigned']))))
        if boundary != 'advance':
            assert session.get(Step, step_id).result_json is None
            assert attempt.response_json['content_discarded'] is True
