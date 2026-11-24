"""Lease, publication and durable-result fault boundaries under duplicate delivery."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
import runpy

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.tests.test_orchestration import factory, seed, drain
from round_robin.database.models import Attempt, BudgetOperation, Meeting, Outbox, PolicyBucket, Run, Step, utcnow
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker import tasks
from round_robin.dispatcher import service


def prepared(factory):
    run_id=seed(factory)
    with factory.begin() as session:
        advance_run(session,lock_work_run(session,run_id))
        step_id=session.scalar(select(Step.id))
    return run_id,step_id


@pytest.mark.parametrize('case,status',[('missing','missing'),('claimed','already_claimed'),('admission','admission_wait'),('wrong_stage','wrong_stage'),('research','research_wait'),('capacity','capacity_wait'),('claims','policy_blocked'),('auditor','policy_blocked'),('dispatch','dispatch_paused'),('provider_gate','provider_blocked')])
def test_worker_rejected_delivery_cannot_send_or_create_new_authorization(factory,monkeypatch,case,status):
    run_id,step_id=prepared(factory)
    with factory.begin() as session:
        run=session.get(Run,run_id);step=session.get(Step,step_id)
        if case=='claimed':step.status='claimed'
        if case=='admission':run.status='queued'
        if case=='wrong_stage':step.stage='audit'
        if case=='research':step.stage='proposal';run.stage='proposals';run.configuration={**run.configuration,'research_state':'running'}
        if case=='claims':step.infrastructure_claims=3
        if case=='auditor':
            step.stage='audit';run.stage='audit'
            session.add(Step(id='owned_synth',run_id=run_id,stage='synthesis',role_slot_id='synthesis',provider='openai',status='succeeded',result_json={'output':{'summary':'Earlier successful result'}}))
        if case=='dispatch':
            policy=session.scalar(select(PolicyBucket).where(PolicyBucket.key=='deployment'));policy.metadata_json={**policy.metadata_json,'dispatch_locked':True}
    if case=='capacity':monkeypatch.setattr(tasks,'provider_capacity_available',lambda *a, **kw:False)
    adapter=SimulatedProvider('openai')
    if case=='provider_gate':
        def gate(*a):raise ProviderFailure('provider_configuration_invalid',may_have_sent=False)
        monkeypatch.setattr(adapter,'conservative_bound',gate)
    result=tasks.execute_step('missing' if case=='missing' else step_id,session_factory=factory,adapter_registry={'openai':adapter})
    assert result['status']==status and adapter.calls==0
    with factory() as session:assert session.scalar(select(Attempt)) is None


def test_authorization_database_failure_is_sanitized_and_never_sends(factory):
    _,step_id=prepared(factory)
    @contextmanager
    def broken():raise SQLAlchemyError('private SQL values');yield
    adapter=SimulatedProvider('openai')
    assert tasks.execute_step(step_id,session_factory=broken,adapter_registry={'openai':adapter})=={'status':'authorization_unconfirmed'}
    assert adapter.calls==0


@pytest.mark.parametrize('kind',['unknown','interrupt','missing_attempt','already_persisted','retry_persistence','unconfirmed_persistence'])
def test_provider_boundary_and_result_persistence_faults_never_resend(factory,monkeypatch,kind):
    run_id,step_id=prepared(factory)
    class Provider(SimulatedProvider):
        def generate(self,request):
            result=super().generate(request)
            if kind=='unknown':raise RuntimeError('private provider text')
            if kind=='interrupt':raise KeyboardInterrupt()
            if kind in {'missing_attempt','already_persisted'}:
                with factory.begin() as session:
                    attempt=session.scalar(select(Attempt))
                    if kind=='missing_attempt':session.delete(attempt)
                    else:attempt.completed_at=utcnow();attempt.usage_certainty='reported'
            return result
    adapter=Provider('openai');entered=[]
    @contextmanager
    def faulted_factory():
        entered.append(1)
        if len(entered)>1 and (kind=='unconfirmed_persistence' or kind=='retry_persistence' and len(entered)<4):
            raise SQLAlchemyError('private query values')
        with factory() as session:yield session
    monkeypatch.setattr(tasks.time,'sleep',lambda _:None)
    if kind=='interrupt':
        with pytest.raises(KeyboardInterrupt):tasks.execute_step(step_id,session_factory=faulted_factory,adapter_registry={'openai':adapter})
    else:
        result=tasks.execute_step(step_id,session_factory=faulted_factory,adapter_registry={'openai':adapter})
        expected={'missing_attempt':'persistence_unconfirmed','already_persisted':'already_persisted','unconfirmed_persistence':'persistence_unconfirmed'}.get(kind,'persisted')
        assert result['status']==expected
        if kind=='unconfirmed_persistence':assert len(entered)==4
        if kind=='missing_attempt':
            with factory() as session:
                operation=session.get(BudgetOperation,result['attempt_id'])
                assert operation.status=='reserved' and operation.settled_usd is None
                assert session.get(Attempt,result['attempt_id']) is None
        if kind=='unknown':
            with factory() as session:
                attempt=session.scalar(select(Attempt))
                assert attempt.status=='unknown' and attempt.usage_certainty=='unknown'
                assert attempt.response_json=={'error_code':'unclassified_provider_failure'}
    assert adapter.calls==1


@pytest.mark.parametrize('case',['missing','wrong_token','eligible','policy_failure','database_failure'])
def test_heartbeat_extends_only_current_eligible_authority(factory,case,monkeypatch):
    run_id,step_id=prepared(factory)
    original=utcnow()+timedelta(seconds=1)
    with factory.begin() as session:
        step=session.get(Step,step_id);step.status='authorized';step.lease_owner='token';step.lease_expires_at=original
    class Stop:
        count=0
        def wait(self,seconds):self.count+=1;return self.count>1
    if case=='policy_failure':
        def deny(*a):raise BudgetDenied('dispatch_paused')
        monkeypatch.setattr(tasks,'check_eligible',deny)
    @contextmanager
    def broken():raise SQLAlchemyError('private SQL');yield
    tasks._heartbeat('missing' if case=='missing' else step_id,'wrong' if case=='wrong_token' else 'token',broken if case=='database_failure' else factory,Stop())
    with factory() as session:
        expires=session.get(Step,step_id).lease_expires_at
        assert (expires.replace(tzinfo=original.tzinfo)>original)==(case=='eligible')


@pytest.mark.parametrize('task_name', ['advance_task','finalize_task','research_task','execute_task'])
def test_celery_entrypoints_have_safe_result_contracts(factory,monkeypatch,task_name):
    from round_robin.worker import research_tasks
    if task_name=='research_task':
        monkeypatch.setattr(research_tasks,'execute_research',lambda run_id:{'status':'done'})
        assert research_tasks.research_task('run_safe')=={'status':'done'}
        def fail(*a):raise SQLAlchemyError('secret SQL')
        monkeypatch.setattr(research_tasks,'execute_research',fail)
        assert research_tasks.research_task('run_safe')['status']=='storage_unavailable'
    elif task_name=='execute_task':
        monkeypatch.setattr(tasks,'execute_step',lambda step_id:{'status':step_id})
        assert tasks.execute_task('step_safe')=={'status':'step_safe'}
    else:
        monkeypatch.setattr(tasks,'SessionLocal',factory)
        function=getattr(tasks,task_name)
        assert function('missing')['status'] in {'advanced','finalized'}
        run_id=seed(factory)
        assert function(run_id)['status'] in {'advanced','finalized'}
        @contextmanager
        def failure():raise SQLAlchemyError('secret SQL');yield
        monkeypatch.setattr(tasks,'SessionLocal',failure)
        assert function(run_id)=={'status':'database_outcome_unconfirmed'}


def test_dispatch_sender_has_only_identifiers_and_no_secret_payload(monkeypatch):
    seen=[]
    monkeypatch.setattr(service.celery_app,'send_task',lambda *a,**kw:seen.append((a,kw)))
    monkeypatch.setenv('ROUND_ROBIN_TASK_QUEUE','queue_owned')
    service._send('execute','step_owned')
    assert seen==[(('round_robin.execute',),{'args':['step_owned'],'queue':'queue_owned','retry':False})]


def test_expired_infrastructure_claims_requeue_only_before_the_original_limit(factory):
    run_id,step_id=prepared(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);step=session.get(Step,step_id)
        step.infrastructure_claims=1;step.status='claimed';step.lease_owner='old'
        service._expire_step(session,run,step)
        assert step.status=='pending' and step.lease_owner is None
        step.infrastructure_claims=3;step.status='claimed'
        service._expire_step(session,run,step)
        assert step.status=='failed' and step.error_code=='infrastructure_claim_limit'


def test_reconciler_expires_input_deadline_and_republishes_only_pending_owned_intents(factory):
    run_id,step_id=prepared(factory)
    with factory.begin() as session:
        run=session.get(Run,run_id);run.status='awaiting_input';run.input_expires_at=utcnow()-timedelta(seconds=1)
    assert service.reconcile_once(session_factory=factory)>=1
    with factory() as session:
        assert session.get(Run,run_id).stop_reason=='input_expired'
    run2=seed(factory,suffix='second')
    with factory.begin() as session:
        run=session.get(Run,run2)
        # Draining prevents an otherwise eligible queued run acquiring admission.
        dep=session.scalar(select(PolicyBucket).where(PolicyBucket.key=='deployment'));dep.metadata_json={**dep.metadata_json,'draining':True}
        session.add(Outbox(id='published_retry',run_id=run2,kind='advance',status='published',published_at=utcnow()-timedelta(seconds=20)))
        session.add(Outbox(id='published_fresh',run_id=run2,kind='advance',status='published',published_at=utcnow()))
    service.reconcile_once(session_factory=factory)
    with factory.begin() as session:
        assert session.get(Outbox,'published_retry').status=='done'
        assert session.get(Outbox,'published_fresh').status=='published'
        active = list(session.scalars(select(Outbox).where(Outbox.run_id==run2, Outbox.kind=='advance', Outbox.status.in_(['pending','published']))))
        assert len(active)==1
        active[0].published_at=utcnow()-timedelta(seconds=20)
    service.reconcile_once(session_factory=factory)
    with factory() as session:
        # The retained singleton still recovers genuinely lost delivery.
        assert session.get(Outbox,'published_fresh').status=='pending'


def test_dispatch_loop_owns_signals_and_sanitizes_maintenance_failure(monkeypatch,caplog):
    from round_robin.worker import image_tasks
    from round_robin.operations import maintenance
    signals=[];calls=[]
    class Stop:
        n=0
        def is_set(self):return self.n>=2
        def set(self):self.n=2
        def wait(self,seconds):self.n+=1
    stop=Stop()
    monkeypatch.setattr(service.threading,'Event',lambda:stop)
    monkeypatch.setattr(service.signal,'signal',lambda signum,handler:signals.append(handler))
    def reconcile():calls.append('reconcile');raise SQLAlchemyError('private statement')
    monkeypatch.setattr(service,'reconcile_once',reconcile)
    monkeypatch.setattr(service.time,'monotonic',lambda:100)
    def cleanup(*a):calls.append('cleanup');raise RuntimeError('private filesystem')
    monkeypatch.setattr(maintenance,'maintenance',cleanup)
    service.main()
    assert calls==['reconcile','cleanup','reconcile']
    assert 'private' not in caplog.text
    assert 'cleanup unavailable' in caplog.text
    assert len(signals)==2
    signals[0]();assert stop.is_set()
    stop.n=0;calls.clear()
    monkeypatch.setattr(service,'reconcile_once',lambda:calls.append('reconcile'))
    monkeypatch.setattr(service,'dispatch_once',lambda:calls.append('dispatch'))
    monkeypatch.setattr(image_tasks,'reconcile_image_operations',lambda:calls.append('images_reconcile'))
    monkeypatch.setattr(image_tasks,'dispatch_pending_image_operations',lambda:calls.append('images_dispatch'))
    monkeypatch.setattr(maintenance,'maintenance',lambda *a:calls.append('cleanup'))
    service.main()
    assert calls.count('images_dispatch')==2 and calls.count('cleanup')==1


def test_dispatcher_module_entrypoint_handles_immediate_stop_without_connecting(monkeypatch):
    monkeypatch.setattr(service.signal,'signal',lambda signum,handler:handler())
    runpy.run_module('round_robin.dispatcher.service',run_name='__main__')


@pytest.mark.parametrize('case',['recent_orphan','expired_orphan','deleted_orphan','orphan_policy_error','run_terminal_race','run_policy_error','other_clock_error','expired_run','future_input','live_lease'])
def test_reconciler_races_retain_uncertainty_and_apply_only_owned_expiry(factory,monkeypatch,case):
    run_id,step_id=prepared(factory)
    now=utcnow()
    with factory.begin() as session:
        run=session.get(Run,run_id);step=session.get(Step,step_id)
        if 'orphan' in case:
            run.status='cancelled';step.status='skipped';step.lease_expires_at=None
            session.add(BudgetOperation(id='attempt_orphan',run_id=run_id,owner_id=run.owner_id,kind='model',status='reserved',reserved_usd='0',authorized_at=now-timedelta(minutes=5),metadata_json={'model_call':True}))
            session.add(Attempt(id='attempt_orphan',run_id=run_id,step_id=step_id,provider='openai',status='authorized',reserved_usd='0',authorized_at=now if case=='recent_orphan' else now-timedelta(minutes=5),configuration={}))
        if case=='expired_run':run.active_seconds=601
        if case=='future_input':run.status='awaiting_input';run.input_expires_at=now+timedelta(minutes=1)
        if case=='live_lease':step.status='claimed';step.lease_owner='owned';step.lease_expires_at=now+timedelta(minutes=1)
    calls=[]
    @contextmanager
    def concurrent_factory():
        calls.append(1)
        if len(calls)==2 and case in {'deleted_orphan','run_terminal_race'}:
            with factory.begin() as session:
                if case=='deleted_orphan':session.delete(session.get(Attempt,'attempt_orphan'))
                else:session.get(Run,run_id).status='failed'
        with factory() as session:yield session
    if case in {'orphan_policy_error','run_policy_error'}:
        def denied(*args):raise BudgetDenied('durability_compromised')
        monkeypatch.setattr(service,'lock_work_run',denied)
    if case=='other_clock_error':
        def denied(*args):raise BudgetDenied('unrelated_invariant')
        monkeypatch.setattr(service,'remaining_active',denied)
    changed=service.reconcile_once(session_factory=concurrent_factory)
    with factory() as session:
        run=session.get(Run,run_id)
        if case=='recent_orphan':assert session.get(Attempt,'attempt_orphan').status=='authorized'
        if case=='expired_orphan':assert changed>=1 and session.get(Attempt,'attempt_orphan').status=='unknown'
        if case=='deleted_orphan':assert session.get(Attempt,'attempt_orphan') is None
        if case=='orphan_policy_error':assert session.get(BudgetOperation,'attempt_orphan').status=='reserved'
        if case=='run_policy_error':assert run.status=='running'
        if case=='expired_run':assert run.stop_reason=='active_deadline_exceeded'
        if case=='future_input':assert run.status=='awaiting_input'
        if case=='live_lease':assert session.get(Step,step_id).lease_owner=='owned'


def test_published_intents_use_step_and_run_state_without_replaying_completed_work(factory,monkeypatch):
    run_id,step_id=prepared(factory)
    monkeypatch.setattr(service,'advance_run',lambda *args:None)
    with factory.begin() as session:
        for identity,kind,step in [('owned_run','advance',None),('pending_step','execute',step_id),('missing_step','execute','step_gone')]:
            session.add(Outbox(id=identity,run_id=run_id,kind=kind,step_id=step,status='published',published_at=utcnow()-timedelta(seconds=20)))
        session.add(Step(id='done_step',run_id=run_id,stage='intake',role_slot_id='other',provider='google',status='succeeded',result_json={}))
        session.add(Outbox(id='settled_step',run_id=run_id,kind='execute',step_id='done_step',status='published',published_at=utcnow()))
        session.add(Outbox(id='gone_run',run_id='run_gone',kind='advance',status='published',published_at=utcnow()))
    service.reconcile_once(session_factory=factory)
    with factory() as session:
        assert session.get(Outbox,'owned_run').status=='done'
        assert session.get(Outbox,'pending_step').status=='pending'
        assert session.get(Outbox,'missing_step').status=='done'
        assert session.get(Outbox,'settled_step').status=='done'
        assert session.get(Outbox,'gone_run').status=='done'


def test_worker_request_time_and_claim_corruption_block_before_send(factory,monkeypatch):
    run_id,step_id=prepared(factory)
    with factory() as session:
        run=session.get(Run,run_id);step=session.get(Step,step_id)
        monkeypatch.setattr(tasks,'remaining_active',lambda _:100)
        with pytest.raises(BudgetDenied,match='finish_time_reserve'):tasks.build_request(session,run,step)
        monkeypatch.setattr(tasks,'remaining_active',lambda _:500)
        from round_robin.research.claims import ClaimRejected
        def broken(*args,**kwargs):raise ClaimRejected('Stored claim corrupt')
        monkeypatch.setattr('round_robin.research.reuse.claim_packet',broken)
        with pytest.raises(BudgetDenied,match='claim_integrity_failed'):tasks.build_request(session,run,step)


def test_claim_persistence_failure_does_not_drop_completed_receipt_or_authorize_retry(factory,monkeypatch):
    run_id,step_id=prepared(factory)
    from round_robin.research.claims import ClaimRejected
    def broken(*args,**kwargs):raise ClaimRejected('Duplicate immutable claim')
    monkeypatch.setattr('round_robin.research.reuse.persist_claims',broken)
    provider=SimulatedProvider('openai')
    result=tasks.execute_step(step_id,session_factory=factory,adapter_registry={'openai':provider})
    assert result['status']=='persisted' and provider.calls==1
    with factory() as session:
        attempt=session.scalar(select(Attempt))
        assert attempt.status=='failed' and attempt.usage_certainty=='reported'
        assert attempt.response_json=={'error_code':'claim_integrity_failed'}
        assert session.get(BudgetOperation,attempt.id).status=='settled'
