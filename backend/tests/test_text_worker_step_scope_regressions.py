from datetime import timedelta
from dataclasses import replace
from decimal import Decimal

import pytest

from sqlalchemy import delete, select, update
from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import Attempt, BudgetOperation, Run, Step, utcnow
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker import tasks


def seed_runs(factory, config=None):
    target=seed(factory,suffix='target',config=config)
    other=seed(factory,suffix='other')
    with factory.begin() as session:
        if config and config.get('mode') == 'live':
            from round_robin.database.services import lock_policies
            for policy in lock_policies(session,'owner_test'):
                policy.spending_locked=False;policy.limit_usd='10';policy.metadata_json={'daily_usd':'10','monthly_usd':'50'}
        advance_run(session,lock_work_run(session,target))
        sid=session.scalar(select(Step.id).where(Step.run_id==target,Step.status=='pending'))
        assert sid
    return target,other,sid


def intercept_rebind(monkeypatch, sid, other, mode="sql"):
    original=tasks.lock_work_run
    def rebind(session,run_id):
        run=original(session,run_id)
        if mode == "sql":
            session.execute(update(Step).where(Step.id==sid).values(run_id=other).execution_options(synchronize_session=False))
        else:
            session.get(Step,sid).run_id=other
        return run
    monkeypatch.setattr(tasks,'lock_work_run',rebind)


@pytest.mark.parametrize("mode", ["sql", "pending_orm"])
def test_step_moved_after_snapshot_cannot_authorize_provider(factory,monkeypatch,mode):
    target,other,sid=seed_runs(factory)
    intercept_rebind(monkeypatch,sid,other,mode)
    class Provider(SimulatedProvider):
        bound_calls=0
        def conservative_bound(self,request):
            self.bound_calls+=1
            return super().conservative_bound(request)
    provider=Provider('openai')
    result=tasks.execute_step(sid,session_factory=factory,adapter_registry={'openai':provider})
    assert provider.bound_calls==0 and provider.calls==0, result
    with factory() as session:
        step=session.get(Step,sid)
        assert step.run_id==other and step.status=='pending'
        assert session.get(Run,target).status=='running'
        assert session.scalar(select(Attempt.id)) is None
        assert session.scalar(select(BudgetOperation.id).where(BudgetOperation.kind=='model')) is None


@pytest.mark.parametrize("mode", ["sql", "pending_orm"])
def test_step_moved_after_snapshot_cannot_extend_lease(factory,monkeypatch,mode):
    _,other,sid=seed_runs(factory)
    old=utcnow()+timedelta(seconds=1)
    with factory.begin() as session:
        step=session.get(Step,sid);step.status='authorized';step.lease_owner='token';step.lease_expires_at=old
    intercept_rebind(monkeypatch,sid,other,mode)
    class Stop:
        count=0
        def wait(self,_):self.count+=1;return self.count>1
    tasks._heartbeat(sid,'token',factory,Stop())
    with factory() as session:
        step=session.get(Step,sid)
        assert step.run_id==other
        assert step.lease_expires_at.replace(tzinfo=old.tzinfo)==old


def test_valid_step_still_completes_once_and_duplicate_does_not_send(factory):
    _,_,sid=seed_runs(factory)
    provider=SimulatedProvider('openai')
    result=tasks.execute_step(sid,session_factory=factory,adapter_registry={'openai':provider})
    assert result['status']=='persisted' and provider.calls==1
    assert tasks.execute_step(sid,session_factory=factory,adapter_registry={'openai':provider})['status']=='settled'
    assert provider.calls==1


@pytest.mark.parametrize('usage', ['known','unknown'])
def test_rebound_after_send_preserves_unconfirmed_reservation_without_resend(factory,monkeypatch,usage):
    from round_robin.api.preflight import run_configuration
    from round_robin.configuration import Settings
    from round_robin.database.services import lock_policies
    from round_robin.providers.base import ProviderFailure
    monkeypatch.setattr('round_robin.orchestration.configuration.verify_frozen_configuration',lambda _:None)
    config=run_configuration(Settings(),'simulated');config.update(mode='live',funded=True)
    target,other,sid=seed_runs(factory,config=config)
    with factory.begin() as session:
        for policy in lock_policies(session,'owner_test'):
            policy.spending_locked=False;policy.limit_usd='10';policy.metadata_json={'daily_usd':'10','monthly_usd':'50'}
    class Provider(SimulatedProvider):
        def conservative_bound(self,request):return Decimal('0.40')
        def generate(self,request):
            result=super().generate(request)
            with factory.begin() as session:session.get(Step,sid).run_id=other
            if usage=='unknown':raise ProviderFailure('timeout',may_have_sent=True)
            return replace(result,cost_usd=Decimal('0.75'),simulated=False,provider_request_id='synthetic')
    provider=Provider('openai')
    outcome=tasks.execute_step(sid,session_factory=factory,adapter_registry={'openai':provider})
    assert outcome['status']=='persistence_unconfirmed' and provider.calls==1
    with factory() as session:
        attempt=session.get(Attempt,outcome['attempt_id']);ledger=session.get(BudgetOperation,attempt.id)
        assert attempt.run_id==target and attempt.completed_at is None and attempt.response_json is None
        assert ledger.run_id==target and ledger.status=='reserved' and ledger.reserved_usd=='0.40' and ledger.settled_usd is None
        assert session.get(Step,sid).run_id==other
    tasks.execute_step(sid,session_factory=factory,adapter_registry={'openai':provider})
    assert provider.calls==1


@pytest.mark.parametrize('heartbeat', [False, True])
def test_step_deleted_after_snapshot_is_safe_noop(factory,monkeypatch,heartbeat):
    target,_,sid=seed_runs(factory)
    if heartbeat:
        with factory.begin() as session:
            step=session.get(Step,sid);step.status='authorized';step.lease_owner='token';step.lease_expires_at=utcnow()+timedelta(seconds=1)
    original=tasks.lock_work_run
    def remove_after_snapshot(session,run_id):
        run=original(session,run_id)
        session.execute(delete(Step).where(Step.id==sid).execution_options(synchronize_session=False))
        return run
    monkeypatch.setattr(tasks,'lock_work_run',remove_after_snapshot)
    provider=SimulatedProvider('openai')
    if heartbeat:
        class Stop:
            count=0
            def wait(self,_):self.count+=1;return self.count>1
        tasks._heartbeat(sid,'token',factory,Stop())
    else:
        assert tasks.execute_step(sid,session_factory=factory,adapter_registry={'openai':provider})['status']=='scope_unavailable'
    assert provider.calls==0
    with factory() as session:
        assert session.get(Step,sid) is None and session.get(Run,target).status=='running'
        assert session.scalar(select(Attempt.id)) is None
