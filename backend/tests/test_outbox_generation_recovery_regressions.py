from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Outbox, Step, utcnow
from round_robin.dispatcher.service import dispatch_once
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run

@pytest.mark.parametrize('status', ['pending', 'published'])
@pytest.mark.parametrize('existing_generation', [0, 1])
def test_advance_recovers_missing_current_generation_without_duplicate(factory, status, existing_generation):
    run_id = seed(factory, suffix='generation')
    with factory.begin() as session:
        run=lock_work_run(session,run_id);advance_run(session,run)
        step=session.scalar(select(Step).where(Step.run_id==run_id, Step.status=='pending')); assert step
        sid=step.id
        for row in session.scalars(select(Outbox)):
            session.delete(row)
        retry_due=utcnow()+timedelta(minutes=2)
        run.configuration={**run.configuration,'step_dispatch':{sid:{'generation':1,'not_before':retry_due.isoformat()}}}
        session.add(Outbox(id='existing',run_id=run_id,step_id=sid,kind='execute',status=status,due_at=utcnow()+timedelta(days=1),published_at=utcnow() if status=='published' else None,payload={'dispatch_generation':existing_generation}))
    for _ in range(2):
        with factory.begin() as session:advance_run(session,lock_work_run(session,run_id))
    with factory() as session:
        rows=list(session.scalars(select(Outbox).where(Outbox.run_id==run_id,Outbox.step_id==sid,Outbox.kind=='execute',Outbox.status.in_(['pending','published']))))
        current=[row for row in rows if row.payload.get('dispatch_generation',0)==1]
        assert len(current)==1
        if existing_generation==1: assert current[0].id=='existing'
        else:
            assert current[0].id!='existing' and current[0].due_at.replace(tzinfo=retry_due.tzinfo)>=retry_due
            assert session.get(Outbox,'existing').status==status
    assert dispatch_once(session_factory=factory,sender=lambda *_:pytest.fail('work sent early'))==0

@pytest.mark.parametrize('payload_case',['missing','nondict','malformed'])
def test_legacy_or_unreadable_payload_does_not_spawn_work(factory, payload_case):
    from sqlalchemy import text
    run_id=seed(factory,suffix='legacy')
    with factory.begin() as session:
        run=lock_work_run(session,run_id);advance_run(session,run)
        step=session.scalar(select(Step).where(Step.run_id==run_id,Step.status=='pending'));sid=step.id
        for row in session.scalars(select(Outbox)):
            session.delete(row)
        payload={} if payload_case in ('missing','malformed') else ['invalid']
        session.add(Outbox(id='existing',run_id=run_id,step_id=sid,kind='execute',status='pending',due_at=utcnow()+timedelta(days=1),payload=payload))
        session.flush()
        if payload_case=='malformed': session.execute(text('UPDATE outbox SET payload=:payload WHERE id=:id'),{'payload':'{broken','id':'existing'})
    with factory.begin() as session:advance_run(session,lock_work_run(session,run_id))
    with factory() as session:
        ids=list(session.scalars(select(Outbox.id).where(Outbox.run_id==run_id,Outbox.step_id==sid,Outbox.kind=='execute',Outbox.status=='pending')))
        assert ids==['existing']
        assert session.get(Step,sid).status=='pending'

@pytest.mark.parametrize('rows', [256,257])
def test_excessive_stale_intents_remain_fenced_instead_of_unbounded_scan(factory,rows):
    run_id=seed(factory,suffix='many')
    with factory.begin() as session:
        run=lock_work_run(session,run_id);advance_run(session,run)
        step=session.scalar(select(Step).where(Step.run_id==run_id,Step.status=='pending'));sid=step.id
        for row in session.scalars(select(Outbox)):session.delete(row)
        run.configuration={**run.configuration,'step_dispatch':{sid:{'generation':1}}}
        session.add_all([Outbox(id=f'stale_{i:03}',run_id=run_id,step_id=sid,kind='execute',status='pending',due_at=utcnow()+timedelta(days=1),payload={'dispatch_generation':0}) for i in range(rows)])
    with factory.begin() as session:advance_run(session,lock_work_run(session,run_id))
    with factory() as session:
        saved=list(session.scalars(select(Outbox).where(Outbox.run_id==run_id,Outbox.step_id==sid,Outbox.kind=='execute',Outbox.status=='pending')))
        assert len(saved)==rows+(rows==256)
        assert sum(row.payload.get('dispatch_generation')==1 for row in saved)==(rows==256)

@pytest.mark.parametrize('bad', [None, True, -1, 2, '0', [], {}, 0.5])
def test_unproven_generation_is_fenced(factory, bad):
    run_id=seed(factory,suffix='bad_gen')
    with factory.begin() as session:
        run=lock_work_run(session,run_id);advance_run(session,run)
        step=session.scalar(select(Step).where(Step.run_id==run_id,Step.status=='pending'));sid=step.id
        for row in session.scalars(select(Outbox)):session.delete(row)
        run.configuration={**run.configuration,'step_dispatch':{sid:{'generation':1}}}
        session.add(Outbox(id='bad',run_id=run_id,step_id=sid,kind='execute',status='pending',due_at=utcnow()+timedelta(days=1),payload={'dispatch_generation':bad}))
    with factory.begin() as session:advance_run(session,lock_work_run(session,run_id))
    with factory() as session:
        assert list(session.scalars(select(Outbox.id).where(Outbox.run_id==run_id,Outbox.step_id==sid,Outbox.kind=='execute',Outbox.status=='pending')))==['bad']
        assert session.get(Step,sid).status=='pending'

@pytest.mark.parametrize('bad_control', [None, [], {'generation':True}, {'generation':-1}, {'generation':'1'}, {'generation':1,'not_before':'broken'}, {'generation':1,'not_before':'2026-01-01T00:00:00'}, {'generation':1,'not_before':42}])
def test_unreadable_current_control_fences_recovery(factory, bad_control):
    run_id=seed(factory,suffix='bad_control')
    with factory.begin() as session:
        run=lock_work_run(session,run_id);advance_run(session,run)
        step=session.scalar(select(Step).where(Step.run_id==run_id,Step.status=='pending'));sid=step.id
        for row in session.scalars(select(Outbox)):session.delete(row)
        run.configuration={**run.configuration,'step_dispatch':{sid:bad_control}}
        session.add(Outbox(id='old',run_id=run_id,step_id=sid,kind='execute',status='pending',due_at=utcnow()+timedelta(days=1),payload={'dispatch_generation':0}))
    with factory.begin() as session:advance_run(session,lock_work_run(session,run_id))
    with factory() as session:
        assert list(session.scalars(select(Outbox.id).where(Outbox.run_id==run_id,Outbox.step_id==sid,Outbox.kind=='execute',Outbox.status=='pending')))==['old']
        assert session.get(Step,sid).status=='pending'
