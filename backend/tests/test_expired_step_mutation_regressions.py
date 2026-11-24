"""Expired leases preserve unrelated work and unknown operation exposure."""

from datetime import timedelta

from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    Event,
    Outbox,
    Run,
    Step,
    utcnow,
)
from round_robin.dispatcher.service import _expire_step
from sqlalchemy import select

from backend.tests.test_exhaustive_runtime_workers import prepared
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_expired_authorization_fences_only_its_unfinished_attempt(factory):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = session.get(Step, step_id)
        for work in session.scalars(select(Outbox).where(Outbox.step_id == step_id, Outbox.kind == "execute")):
            work.status = "done"
        step.status, step.lease_owner, step.lease_expires_at = "authorized", "worker_lost", utcnow() - timedelta(seconds=1)
        other = Step(id="step_other", run_id=run_id, stage="audit", role_slot_id="independent", provider="google", status="authorized")
        session.add(other)
        session.flush()
        rows = [
            ("attempt_target", step_id, "authorized", None, "reserved"),
            ("attempt_other", other.id, "authorized", None, "reserved"),
            # Recovery can encounter partial/inconsistent historical states and
            # must not rewrite a non-authorized or already-completed record.
            ("attempt_non_authorized", step_id, "failed", None, "unknown"),
            ("attempt_completed", step_id, "authorized", utcnow(), "unknown"),
        ]
        for identity, owner_step, status, completed, ledger_status in rows:
            session.add(Attempt(id=identity, run_id=run_id, step_id=owner_step, provider="openai", status=status, completed_at=completed, reserved_usd="0.42", usage_certainty="reserved"))
            session.add(BudgetOperation(id=identity, run_id=run_id, owner_id=run.owner_id, kind="model", status=ledger_status, reserved_usd="0.42", metadata_json={"model_call": True}))
        session.flush()
        _expire_step(session, run, step)
    with factory() as session:
        step = session.get(Step, step_id)
        attempt = session.get(Attempt, "attempt_target")
        ledger = session.get(BudgetOperation, "attempt_target")
        assert (step.status, step.error_code, step.lease_owner, step.lease_expires_at) == ("failed", "worker_lost_after_authorization", None, None)
        assert attempt.status == "unknown" and attempt.usage_certainty == "unknown" and attempt.completed_at is not None
        assert ledger.status == "unknown" and ledger.reserved_usd == "0.42" and ledger.settled_usd is None
        other = session.get(Attempt, "attempt_other")
        assert other.status == "authorized" and other.completed_at is None
        assert session.get(BudgetOperation, "attempt_other").status == "reserved"
        assert session.get(Attempt, "attempt_non_authorized").status == "failed"
        assert session.get(Attempt, "attempt_completed").status == "authorized"
        event = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == "attempt.outcome_unknown"))
        assert event is not None and event.payload == {"step_id": step_id, "attempt_ids": ["attempt_target"]}
        assert not list(session.scalars(select(Outbox).where(Outbox.run_id == run_id, Outbox.kind == "execute", Outbox.step_id == step_id, Outbox.status == "pending")))


def test_expired_pre_authorization_claim_requeues_exact_step(factory):
    run_id, step_id = prepared(factory)
    with factory.begin() as session:
        run, step = session.get(Run, run_id), session.get(Step, step_id)
        for work in session.scalars(select(Outbox).where(Outbox.step_id == step_id, Outbox.kind == "execute")):
            work.status = "done"
        step.status, step.infrastructure_claims, step.lease_owner = "claimed", 1, "worker_lost"
        _expire_step(session, run, step)
    with factory() as session:
        step = session.get(Step, step_id)
        work = list(session.scalars(select(Outbox).where(Outbox.run_id == run_id, Outbox.kind == "execute", Outbox.step_id == step_id, Outbox.status == "pending")))
        assert step.status == "pending" and step.lease_owner is None
        assert len(work) == 1 and work[0].payload["run_id"] == run_id
