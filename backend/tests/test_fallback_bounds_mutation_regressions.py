from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    Event,
    Run,
    Step,
    utcnow,
)
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.policies import plan_recovery
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("failure,elapsed,expected_pending,expected_fallback_calls", [
    ("authentication", 350, False, 0),
    ("timeout", 350, False, 0),
    ("authentication", 0, True, 1),
])
def test_worker_fallback_respects_mandatory_time_reserve(factory, failure, elapsed, expected_pending, expected_fallback_calls):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        run = session.get(Run, run_id)
        run.active_seconds, run.active_started_at = elapsed, utcnow()
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    first = SimulatedProvider("openai", failure)
    second = SimulatedProvider("anthropic")
    registry = {"openai": first, "anthropic": second, "google": SimulatedProvider("google")}
    execute_step(step_id, session_factory=factory, adapter_registry=registry)
    with factory() as session:
        step = session.get(Step, step_id)
        assert (step.status == "pending") is expected_pending
        if expected_pending:
            assert step.provider == "anthropic"
    if expected_pending:
        execute_step(step_id, session_factory=factory, adapter_registry=registry)
    assert first.calls == 1 and second.calls == expected_fallback_calls


@pytest.mark.parametrize("stage,required", [("intake", 305), ("synthesis", 210), ("audit", 115)])
@pytest.mark.parametrize("capacity_delay", [0, 10])
def test_fallback_time_boundary_includes_request_tail_and_capacity(stage, required, capacity_delay):
    configuration = {"limits": {"active_seconds": 600}, "finish_policy": {"reserved_active_seconds": 190},
                     "fallbacks": {stage: ["openai", "anthropic"]}}
    step = SimpleNamespace(stage=stage, provider="openai", calls=1, infrastructure_claims=1)
    run = SimpleNamespace(configuration=configuration, active_seconds=600 - required - capacity_delay, active_started_at=None)
    failure = ProviderFailure("authentication", retryable=False)
    assert not plan_recovery(run, step, failure, synthesizer="google", capacity_delay_seconds=capacity_delay).retry
    run.active_seconds -= 0.001
    result = plan_recovery(run, step, failure, synthesizer="google", capacity_delay_seconds=capacity_delay)
    assert result.retry and result.fallback_provider == "anthropic" and result.delay_seconds == 0


@pytest.mark.parametrize("occupied,expected_pending", [(False, True), (True, False)])
def test_worker_accounts_for_an_existing_capacity_lease(factory, occupied, expected_pending):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        run = session.get(Run, run_id)
        run.active_seconds, run.active_started_at = 280, utcnow()
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
        if occupied:
            session.add(Step(id="occupied_step", run_id=run_id, stage="proposal", role_slot_id="google", provider="google", status="authorized", lease_expires_at=utcnow() + timedelta(seconds=30)))
            session.flush()
            session.add(Attempt(id="occupied_attempt", run_id=run_id, step_id="occupied_step", provider="google", status="authorized"))
    first = SimulatedProvider("openai", "authentication")
    execute_step(step_id, session_factory=factory, adapter_registry={"openai": first, "anthropic": SimulatedProvider("anthropic")})
    assert first.calls == 1
    with factory() as session:
        assert (session.get(Step, step_id).status == "pending") is expected_pending


@pytest.mark.parametrize("failure", ["authentication", "timeout"])
@pytest.mark.parametrize("reported", [False, True])
def test_clock_failure_during_recovery_preserves_known_or_unknown_spend(factory, monkeypatch, failure, reported):
    from round_robin.orchestration import policies
    from round_robin.orchestration.budget import BudgetDenied

    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))

    def clock_failure(run):
        raise BudgetDenied("clock_regression")

    monkeypatch.setattr(policies, "remaining_active", clock_failure)

    class FailingProvider(SimulatedProvider):
        def generate(self, request):
            self.calls += 1
            raise ProviderFailure(failure, retryable=failure == "timeout", may_have_sent=True,
                                  cost_usd=Decimal(0) if reported else None, provider_request_id="synthetic_request")

    first, fallback = FailingProvider("openai"), SimulatedProvider("anthropic")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": first, "anthropic": fallback})
    assert result["status"] == "persisted" and first.calls == 1 and fallback.calls == 0
    with factory() as session:
        step = session.get(Step, step_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert step.status != "pending" and step.provider == "openai"
        assert attempt.provider_request_id == "synthetic_request"
        assert attempt.usage_certainty == ("reported" if reported else "unknown")
        assert ledger.status == ("settled" if reported else "unknown")
        assert not list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type.in_(["step.retry_scheduled", "role.reassigned"]))))
