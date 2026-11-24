from datetime import timedelta

import pytest
from round_robin.database.models import Attempt, BudgetOperation, Run, Step, utcnow
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.orchestration import admission
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


def setup_intake_with_visual_record(factory, payload):
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        run = session.get(Run, run_id)
        session.add(DesignSession(id="design_synthetic", meeting_id=run.meeting_id, owner_id=run.owner_id, mode="diagram", frozen_brief={}))
        session.flush()
        session.add(DesignOperation(id="synthetic_visual_operation", session_id="design_synthetic", provider="google", payload=payload))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    return run_id, step_id


@pytest.mark.parametrize("payload", [None, [], {"status": "authorized"}, {"status": "authorized", "lease_expires_at": "invalid"}, {}])
def test_uninspectable_capacity_before_authorization_fails_without_provider_call(factory, payload):
    run_id, step_id = setup_intake_with_visual_record(factory, payload)
    provider = SimulatedProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    assert result == {"status": "policy_blocked", "code": "capacity_state_invalid"}
    assert provider.calls == 0
    with factory() as session:
        assert session.scalar(select(Attempt).where(Attempt.step_id == step_id)) is None
        assert not list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id, BudgetOperation.kind == "model")))
        assert session.get(Run, run_id).stop_reason == "capacity_state_invalid"
        assert session.get(DesignOperation, "synthetic_visual_operation").payload == payload


@pytest.mark.parametrize("status", ["queued", "reserved", "completed", "failed", "fenced", "released"])
def test_inspectable_inactive_visual_records_do_not_consume_capacity(factory, status):
    _, step_id = setup_intake_with_visual_record(factory, {"status": status})
    provider = SimulatedProvider("openai")
    result = execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    assert result["status"] == "persisted" and provider.calls == 1


@pytest.mark.parametrize("seconds,expected", [(-1, 0), (30, 30)])
def test_well_formed_authorized_visual_lease_uses_its_remaining_time(factory, monkeypatch, seconds, expected):
    now = utcnow()
    setup_intake_with_visual_record(factory, {"status": "authorized", "lease_expires_at": (now + timedelta(seconds=seconds)).isoformat()})
    monkeypatch.setattr(admission, "utcnow", lambda: now)
    with factory() as session:
        assert admission.capacity_delay_seconds(session) == expected
        assert admission.provider_capacity_available(session, "openai") is True
