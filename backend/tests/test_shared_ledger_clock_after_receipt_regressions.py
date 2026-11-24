from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.app import create_app
from round_robin.api.auth import require_owner
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Event, Run, Step
from round_robin.orchestration import admission, budget
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run, finalize_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker import tasks


@pytest.mark.parametrize('source', ['check_eligible', 'remaining_active', 'content_active'])
@pytest.mark.parametrize('outcome', ['success', 'reported_failure', 'unknown_failure'])
@pytest.mark.parametrize('persistent', [False, True])
def test_post_send_clock_jump_keeps_usage_and_permanently_fences(factory, monkeypatch, source, outcome, persistent):
    if source == 'content_active' and outcome != 'success':
        pytest.skip('The content clock is sampled only for a successful result')
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == 'intake'))
    after = {'provider': False, 'seen': 0}
    module = admission if source == 'check_eligible' else tasks
    attribute = 'active_elapsed' if source == 'content_active' else 'remaining_active'
    original = getattr(module, attribute)
    def clocked(run, *args, **kwargs):
        if after['provider']:
            after['seen'] += 1
            if persistent or after['seen'] == 1:
                raise budget.BudgetDenied('clock_regression')
        return original(run, *args, **kwargs)
    monkeypatch.setattr(module, attribute, clocked)
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            after['provider'] = True
            if outcome == 'reported_failure': raise ProviderFailure('authentication', cost_usd=Decimal(0), provider_request_id='synthetic')
            if outcome == 'unknown_failure': raise ProviderFailure('timeout', may_have_sent=True, provider_request_id='synthetic')
            return result
    provider = Provider('openai')
    tasks.execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
    assert provider.calls == 1 and after['seen'] >= 1
    with factory() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert run.status == 'failed' and run.stop_reason == 'clock_regression'
        assert run.configuration.get('active_clock_invalid') is True and run.active_started_at is None
        assert ledger.status == ('unknown' if outcome == 'unknown_failure' else 'settled')
        assert attempt.usage_certainty == ('unknown' if outcome == 'unknown_failure' else 'reported')
        assert attempt.completed_at is not None
        assert step.status == 'failed'
        assert not list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type.in_(['step.retry_scheduled', 'role.reassigned', 'content.committed']))))


@pytest.mark.parametrize('outcome', ['success', 'unknown_failure'])
def test_cancel_during_call_preserves_cancel_and_receipt_even_if_clock_unusable(factory, monkeypatch, tmp_path, outcome):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == 'intake'))
    app = create_app(str(factory.kw['bind'].url), settings=replace(Settings(), artifact_directory=tmp_path / 'private'))
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    after = {'cancel': False}
    real = tasks.remaining_active
    def clocked(run, *args, **kwargs):
        if after['cancel']: raise budget.BudgetDenied('clock_regression')
        return real(run, *args, **kwargs)
    monkeypatch.setattr(tasks, 'remaining_active', clocked)
    with TestClient(app) as client:
        class Provider(SimulatedProvider):
            def generate(self, request):
                result = super().generate(request)
                with factory() as session: version = session.get(Run, run_id).version
                response = client.post(f'/api/runs/{run_id}/cancel', json={'command_id': str(uuid4()), 'expected_run_version': version})
                assert response.status_code == 200
                after['cancel'] = True
                if outcome == 'unknown_failure': raise ProviderFailure('timeout', may_have_sent=True)
                return result
        provider = Provider('openai')
        result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
        assert result['status'] == 'late_result_fenced' and provider.calls == 1
    with factory.begin() as session:
        run = session.get(Run, run_id)
        assert run.status == 'cancel_requested'
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert ledger.status == ('unknown' if outcome == 'unknown_failure' else 'settled')
        assert attempt.response_json.get('content_discarded') is True
        finalize_run(session, run)
        assert run.status == 'cancelled'
