from decimal import Decimal

import pytest
from round_robin.database.models import Event, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


def intake(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    return run_id, step_id


@pytest.mark.parametrize("failure,attempts", [("refusal", 1), ("authentication", 2)])
def test_worker_does_not_fallback_after_refusal_or_exhausting_logical_calls(factory, failure, attempts):
    run_id, step_id = intake(factory)
    registry = {provider: SimulatedProvider(provider, failure) for provider in ("openai", "anthropic", "google")}
    for _ in range(attempts):
        execute_step(step_id, session_factory=factory, adapter_registry=registry)
    with factory() as session:
        step = session.get(Step, step_id)
        assert step.status == "failed"
        assert step.calls == attempts
        events = list(session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == "role.reassigned")))
        assert len(events) == attempts - 1
    assert registry["openai"].calls == 1
    assert registry["anthropic"].calls == attempts - 1
    assert registry["google"].calls == 0


def test_worker_persists_provider_retry_after_in_retry_schedule(factory):
    run_id, step_id = intake(factory)

    class Throttled(SimulatedProvider):
        def generate(self, request):
            raise ProviderFailure("rate_limit", retryable=True, retry_after=17, cost_usd=Decimal(0))

    execute_step(step_id, session_factory=factory, adapter_registry={"openai": Throttled("openai")})
    with factory() as session:
        step = session.get(Step, step_id)
        assert step.status == "pending" and step.calls == 1
        event = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == "step.retry_scheduled"))
        assert event.payload["retry_after_seconds"] == 17
