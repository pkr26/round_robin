"""Clock regression during recovery cannot erase a provider receipt."""
from datetime import timedelta
from decimal import Decimal

import pytest
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    Event,
    ReportVersion,
    Run,
    Step,
    utcnow,
)
from round_robin.orchestration import budget
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("code", ["authentication", "timeout"])
@pytest.mark.parametrize("reported", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
def test_wall_clock_jump_in_recovery_preserves_settlement_and_finalizes(factory, monkeypatch, code, reported, persistent):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    seen = {"after_provider": False, "post_calls": 0}
    real_utcnow = utcnow
    def clock():
        if seen["after_provider"]:
            seen["post_calls"] += 1
            if seen["post_calls"] == 3 or persistent and seen["post_calls"] > 3:
                return real_utcnow() - timedelta(seconds=10)
        return real_utcnow()
    monkeypatch.setattr(budget, "utcnow", clock)
    class FailingProvider(SimulatedProvider):
        def generate(self, request):
            self.calls += 1
            seen["after_provider"] = True
            raise ProviderFailure(code, retryable=code == "timeout", may_have_sent=True,
                                  cost_usd=Decimal(0) if reported else None, provider_request_id="synthetic_request")
    first, fallback = FailingProvider("openai"), SimulatedProvider("anthropic")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": first, "anthropic": fallback})
    assert result["status"] == "persisted" and first.calls == 1 and fallback.calls == 0
    assert seen["post_calls"] >= 3
    with factory() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert attempt.completed_at is not None and attempt.provider_request_id == "synthetic_request"
        assert (attempt.status, attempt.usage_certainty, ledger.status) == (("failed", "reported", "settled") if reported else ("unknown", "unknown", "unknown"))
        assert run.status == "failed" and run.stop_reason == "clock_regression"
        assert run.configuration.get("active_clock_invalid") is True and run.active_started_at is None
        assert step.status == "failed" and step.provider == "openai" and step.calls == 1
        assert not list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type.in_(["step.retry_scheduled", "role.reassigned"]))))
        from round_robin.reports.read_adapters import read_runtime_report
        saved = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        usage = read_runtime_report(session, saved)["usage"]
        assert usage["status"] == ("reported" if reported else "unknown")
        assert (usage["actual_cost"] is None) is (not reported)
        monkeypatch.setattr(budget, "utcnow", real_utcnow)
        with pytest.raises(budget.BudgetDenied, match="clock_regression"):
            budget.remaining_active(run)


def test_one_shot_clock_regression_after_open_circuit_preserves_no_send_and_permanent_fence(factory, monkeypatch):
    from round_robin.database.models import PolicyBucket
    from round_robin.orchestration import circuits
    from round_robin.worker import tasks
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        run = session.get(Run, run_id)
        run.active_seconds, run.active_started_at = 12.5, utcnow()
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
        scope = circuits._scope(run, "openai")
        circuit = {"open_until": (utcnow() + timedelta(seconds=30)).isoformat(), "probe_in_flight": False}
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        bucket.metadata_json = {**bucket.metadata_json, "circuits": {scope: circuit}}
    state = {"pending": False, "jumped": False}
    before = tasks.before_attempt
    def observe_before(*args, **kwargs):
        try:
            return before(*args, **kwargs)
        except ProviderFailure:
            state["pending"] = True
            raise
    def clock():
        if state["pending"] and not state["jumped"]:
            state["jumped"] = True
            return utcnow() - timedelta(seconds=10)
        return utcnow()
    monkeypatch.setattr(tasks, "before_attempt", observe_before)
    monkeypatch.setattr(budget, "utcnow", clock)
    first, fallback = SimulatedProvider("openai"), SimulatedProvider("anthropic")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": first, "anthropic": fallback})
    assert result == {"status": "provider_blocked", "code": "circuit_open"}
    assert state["jumped"] and first.calls == fallback.calls == 0
    with factory() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        assert run.status == "failed" and run.stop_reason == "clock_regression"
        assert run.active_started_at is None and run.active_seconds == 12.5
        assert run.configuration.get("active_clock_invalid") is True and step.calls == 0
        monkeypatch.setattr(budget, "utcnow", utcnow)
        with pytest.raises(budget.BudgetDenied, match="clock_regression"):
            budget.remaining_active(run)
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert not [op for op in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id)) if op.metadata_json.get("model_call") is True]
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert bucket.metadata_json["circuits"][scope] == circuit
        assert not list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type.in_(["step.retry_scheduled", "role.reassigned"]))))
