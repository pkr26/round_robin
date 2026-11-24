from datetime import timedelta
import pytest
from sqlalchemy import select
from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Outbox, Step, utcnow
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run

@pytest.mark.parametrize('status', ['pending', 'published'])
@pytest.mark.parametrize('same_run', [False, True])
def test_foreign_work_cannot_hide_pending_step(factory, status, same_run):
    target = seed(factory, suffix='target')
    other = seed(factory, suffix='other')
    with factory.begin() as session:
        run = lock_work_run(session, target)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == target, Step.status == 'pending'))
        assert step is not None
        step_id = step.id
        for row in session.scalars(select(Outbox).where(Outbox.run_id == target, Outbox.step_id == step_id)):
            session.delete(row)
        session.add(Outbox(id='probe', run_id=target if same_run else other, step_id=step_id, kind='execute', status=status,
                           due_at=utcnow()+timedelta(days=1), published_at=utcnow() if status == 'published' else None, payload={}))
    with factory.begin() as session:
        run = lock_work_run(session, target)
        advance_run(session, run)
    with factory() as session:
        rows = list(session.scalars(select(Outbox).where(Outbox.run_id == target, Outbox.step_id == step_id, Outbox.kind == 'execute', Outbox.status.in_(['pending','published']))))
        assert len(rows) == 1
        assert (rows[0].id == 'probe') is same_run
        assert session.get(Outbox, 'probe').status == status

def test_recreated_execute_obeys_retry_generation_and_due_time(factory):
    target = seed(factory, suffix='retry')
    other = seed(factory, suffix='other')
    with factory.begin() as session:
        run = lock_work_run(session, target)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == target, Step.status == 'pending'))
        step_id = step.id
        for row in session.scalars(select(Outbox).where(Outbox.run_id == target, Outbox.step_id == step_id)):
            session.delete(row)
        due = utcnow() + timedelta(minutes=2)
        run.configuration = {**run.configuration, 'step_dispatch': {step_id: {'generation': 7, 'not_before': due.isoformat()}}}
        session.add(Outbox(id='foreign', run_id=other, step_id=step_id, kind='execute', status='pending', due_at=due, payload={}))
    for _ in range(2):
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, target))
    with factory() as session:
        rows = list(session.scalars(select(Outbox).where(Outbox.run_id == target, Outbox.step_id == step_id, Outbox.kind == 'execute', Outbox.status == 'pending')))
        assert len(rows) == 1
        assert rows[0].payload['dispatch_generation'] == 7
        assert rows[0].due_at.replace(tzinfo=due.tzinfo) >= due
        assert session.get(Outbox, 'foreign').status == 'pending'
        assert session.get(Step, step_id).status == 'pending'

def test_recovered_own_work_dispatches_without_touching_foreign_timer_or_ledger(factory):
    from round_robin.database.models import BudgetOperation
    from round_robin.dispatcher.service import dispatch_once
    target = seed(factory, suffix='deliver')
    other = seed(factory, suffix='foreign')
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, target))
        step = session.scalar(select(Step).where(Step.run_id == target, Step.status == 'pending'))
        step_id = step.id
        for row in session.scalars(select(Outbox)):
            session.delete(row)
        session.add(Outbox(id='foreign', run_id=other, step_id=step_id, kind='execute', status='pending', due_at=utcnow()+timedelta(days=1), payload={}))
        initial_ledger = [(row.id, row.status, row.reserved_usd) for row in session.scalars(select(BudgetOperation).order_by(BudgetOperation.id))]
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, target))
    sent = []
    assert dispatch_once(session_factory=factory, sender=lambda *args: sent.append(args)) == 1
    assert sent == [('execute', {'step_id': step_id, 'dispatch_generation': 0})]
    with factory() as session:
        assert session.get(Outbox, 'foreign').status == 'pending'
        assert session.get(Step, step_id).status == 'pending'
        assert [(row.id, row.status, row.reserved_usd) for row in session.scalars(select(BudgetOperation).order_by(BudgetOperation.id))] == initial_ledger
