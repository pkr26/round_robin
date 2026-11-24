from datetime import timedelta

import pytest
from sqlalchemy import select, update

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Run, Step, utcnow
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.worker import tasks

class Stop:
    count=0
    def wait(self, seconds):
        self.count += 1
        return self.count>1

@pytest.mark.parametrize('case',['queued_run','changed_token','changed_status'])
def test_heartbeat_does_not_extend_fenced_or_stale_authority(factory,monkeypatch,case):
    rid=seed(factory)
    old=utcnow()+timedelta(seconds=1)
    with factory.begin() as session:
        advance_run(session,lock_work_run(session,rid))
        run=session.get(Run,rid)
        step=session.scalar(select(Step).where(Step.run_id==rid,Step.status=='pending'))
        assert step
        sid=step.id;step.status='authorized';step.lease_owner='token';step.lease_expires_at=old
        if case=='queued_run':run.status='queued'
    if case!='queued_run':
        original=tasks.lock_work_run
        def mutate_after_snapshot(session,run_id):
            locked=original(session,run_id)
            change={'lease_owner':'new_token'} if case=='changed_token' else {'status':'failed'}
            session.execute(update(Step).where(Step.id==sid).values(**change).execution_options(synchronize_session=False))
            return locked
        monkeypatch.setattr(tasks,'lock_work_run',mutate_after_snapshot)
    tasks._heartbeat(sid,'token',factory,Stop())
    with factory() as session:
        step=session.get(Step,sid)
        assert step.lease_expires_at.replace(tzinfo=old.tzinfo)==old
        if case=='changed_token':assert step.lease_owner=='new_token'
        if case=='changed_status':assert step.status=='failed'


def test_heartbeat_wait_is_finite_and_shorter_than_lease(factory):
    rid=seed(factory)
    with factory.begin() as session:
        advance_run(session,lock_work_run(session,rid))
        step=session.scalar(select(Step).where(Step.run_id==rid,Step.status=='pending'))
        assert step
        sid=step.id;step.status='authorized';step.lease_owner='token';step.lease_expires_at=utcnow()+timedelta(seconds=1)
    class BoundedStop:
        calls=0
        def wait(self, seconds):
            assert seconds is not None and 0 < seconds < 150
            self.calls+=1
            return self.calls>1
    stop=BoundedStop()
    tasks._heartbeat(sid,'token',factory,stop)
    assert stop.calls==2
