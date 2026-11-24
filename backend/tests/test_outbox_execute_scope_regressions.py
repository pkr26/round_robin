from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Meeting, Outbox, Owner, Run, Step, utcnow
from round_robin.dispatcher.service import dispatch_once, reconcile_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run


@pytest.mark.parametrize("corruption", ["foreign_step", "missing_step", "wrong_meeting_owner"])
@pytest.mark.parametrize("published", [False, True])
def test_corrupt_outbox_never_publishes_and_valid_row_still_progresses(factory, corruption, published):
    target = seed(factory, suffix="target")
    unrelated = seed(factory, suffix="unrelated")
    with factory.begin() as session:
        session.add(Owner(id="owner_other"))
        session.flush()
        advance_run(session, lock_work_run(session, target))
        session.flush()
        step = session.scalar(select(Step).where(Step.run_id == target, Step.status == "pending"))
        assert step is not None
        step_id = step.id
        for item in session.scalars(select(Outbox)):
            item.status = "done"
        if corruption == "foreign_step":
            bad_run, bad_step = unrelated, step_id
        elif corruption == "missing_step":
            bad_run, bad_step = unrelated, "step_missing"
        else:
            bad_run, bad_step = unrelated, "step_wrong_owner"
            session.add(Step(id=bad_step, run_id=unrelated, stage="intake", role_slot_id="intake", provider="openai", status="pending"))
            meeting = session.get(Meeting, session.get(Run, unrelated).meeting_id)
            meeting.owner_id = "owner_other"
        if published:
            # Avoid unrelated run advancement while exercising stale-outbox repair.
            for run in (session.get(Run, target), session.get(Run, unrelated)):
                run.status = "awaiting_input"
                run.input_expires_at = utcnow() + timedelta(minutes=10)
        status = "published" if published else "pending"
        when = utcnow() - timedelta(seconds=30) if published else None
        session.add(Outbox(id="bad", run_id=bad_run, step_id=bad_step, kind="execute", status=status, payload={"dispatch_generation": 0}, published_at=when))
        session.add(Outbox(id="valid", run_id=target, step_id=step_id, kind="execute", status=status, payload={"dispatch_generation": 0}, published_at=when))
    if published:
        reconcile_once(session_factory=factory)
        with factory() as session:
            expected = "done" if corruption == "missing_step" else "cancelled"
            assert session.get(Outbox, "bad").status == expected
            assert session.get(Outbox, "valid").status == "pending"
    sent = []
    assert dispatch_once(session_factory=factory, sender=lambda kind, payload: sent.append((kind, payload))) == 1
    assert sent == [("execute", {"step_id": step_id, "dispatch_generation": 0})]
    with factory() as session:
        expected = "done" if published and corruption == "missing_step" else "cancelled"
        assert session.get(Outbox, "bad").status == expected
        assert session.get(Outbox, "valid").status == "published"
        assert session.get(Step, step_id).status == "pending"
