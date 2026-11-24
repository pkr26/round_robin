from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Outbox, Step, utcnow
from round_robin.dispatcher.service import dispatch_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run


def test_recreated_future_retry_is_not_dispatched_early_and_not_duplicated(factory):
    target, foreign = seed(factory, suffix="target"), seed(factory, suffix="foreign")
    with factory.begin() as session:
        run = lock_work_run(session, target)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == target, Step.status == "pending"))
        step_id = step.id
        for item in session.scalars(select(Outbox)):
            session.delete(item)
        due = utcnow() + timedelta(minutes=2)
        run.configuration = {**run.configuration, "step_dispatch": {step_id: {"generation": 7, "not_before": due.isoformat()}}}
        session.add(Outbox(id="foreign", run_id=foreign, step_id=step_id, kind="execute", status="pending", payload={"dispatch_generation": 0}, due_at=due))
    for _ in range(3):
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, target))
    assert dispatch_once(session_factory=factory, sender=lambda *args: pytest.fail("early dispatch")) == 0
    with factory() as session:
        rows = list(session.scalars(select(Outbox).where(Outbox.run_id == target, Outbox.step_id == step_id, Outbox.status == "pending")))
        assert len(rows) == 1
        assert rows[0].payload["dispatch_generation"] == 7
        assert rows[0].due_at.replace(tzinfo=due.tzinfo) >= due
        assert session.get(Step, step_id).status == "pending"
