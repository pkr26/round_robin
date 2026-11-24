"""Boundary probes for original spend limits, leases and retry authority."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory, seed, drain
from round_robin.database.models import BudgetOperation, Meeting, Run, Step, utcnow
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import admit_run, check_eligible, lock_work_run
from round_robin.orchestration.budget import BudgetDenied, amount, authorize_operation, operations_exposure, reconcile_operation, _policy_checks
from round_robin.orchestration.controller import advance_run
from round_robin.orchestration.policies import plan_recovery, retryable_steps
from round_robin.orchestration.circuits import before_attempt, record_outcome, _scope
from round_robin.providers.base import ProviderFailure


@pytest.mark.parametrize('value', ['NaN','Infinity','-1',True,None,{},'not-money'])
def test_budget_rejects_ambiguous_or_negative_money(value):
    with pytest.raises(BudgetDenied, match='invalid_money'): amount(value)


def running(factory):
    run_id=seed(factory)
    with factory.begin() as session:
        advance_run(session,lock_work_run(session,run_id))
        step_id=session.scalar(select(Step.id))
    return run_id,step_id


@pytest.mark.parametrize('case,code', [('missing_run','run_ineligible'),('deleted_meeting','run_fenced'),('unknown_price','unknown_price'),('duplicate','operation_already_authorized'),('foreign_step','foreign_step'),('invalid_role','invalid_finish_role'),('step_limit','step_call_limit'),('request_limit','finish_call_reserve'),('time_limit','finish_time_reserve'),('finish_money','finish_money_reserve')])
def test_authorization_rejects_one_invalid_dimension_without_any_new_reservation(factory,case,code):
    run_id,step_id=running(factory)
    foreign=seed(factory,suffix='foreign') if case=='foreign_step' else None
    with factory.begin() as session:
        run=lock_work_run(session,run_id);step=session.get(Step,step_id)
        bound='0'; role=None
        if case=='missing_run': run.status='queued'
        if case=='deleted_meeting': session.get(Meeting,run.meeting_id).deleted_at=utcnow()
        if case=='unknown_price': bound=None
        if case=='duplicate': authorize_operation(session,run,'attempt_boundary','model','0',step=step)
        if case=='foreign_step':
            step=Step(id='step_foreign',run_id=foreign,stage='intake',role_slot_id='intake',provider='openai',status='pending')
            session.add(step);session.flush()
        if case=='invalid_role': role='proposal'
        if case=='step_limit': step.calls=2
        if case=='request_limit': run.configuration={**run.configuration,'limits':{**run.configuration['limits'],'max_requests':2}}
        if case=='time_limit': run.active_seconds=500
        if case=='finish_money': bound='1';role='synthesis'
        count=len(list(session.scalars(select(BudgetOperation))))
        with pytest.raises(BudgetDenied,match=code):
            authorize_operation(session,run,'attempt_boundary','model',bound,step=step,finish_role=role)
        assert len(list(session.scalars(select(BudgetOperation))))==count


def test_policy_checks_respect_periods_and_carry_uncertain_exposure(factory,monkeypatch):
    run_id,_=running(factory)
    now=utcnow()
    older=now-timedelta(days=40)
    rows=[BudgetOperation(id='old',status='settled',reserved_usd='3',settled_usd='2',authorized_at=older,metadata_json={}),BudgetOperation(id='current',status='settled',reserved_usd='1',settled_usd='1',authorized_at=now,metadata_json={}),BudgetOperation(id='unknown',status='unknown',reserved_usd='3',settled_usd='4',authorized_at=older,metadata_json={'conservative_booking_usd':'5'}),BudgetOperation(id='released',status='released',reserved_usd='8',authorized_at=now,metadata_json={})]
    assert operations_exposure(rows,now=now)==8
    assert operations_exposure(rows,now=now,period='daily')==6
    assert operations_exposure(rows,now=now,period='monthly')==6
    with factory.begin() as session:
        run=lock_work_run(session,run_id)
        deployment,owner=lock_policies(session,run.owner_id)
        with pytest.raises(BudgetDenied,match='simulated_cost_must_be_zero'):
            _policy_checks(session,run,Decimal('0.01'))
        monkeypatch.setattr('round_robin.orchestration.configuration.verify_frozen_configuration',lambda *a:None)
        run.configuration={**run.configuration,'mode':'live','funded':True}
        with pytest.raises(BudgetDenied,match='spending_locked'):
            _policy_checks(session,run,Decimal('0'))
        deployment.spending_locked=owner.spending_locked=False
        for obj in [deployment,owner]: obj.metadata_json={**obj.metadata_json,'daily_usd':'10','monthly_usd':'50'}
        with pytest.raises(BudgetDenied,match='run_budget_exhausted'):
            _policy_checks(session,run,Decimal('3'))
        for obj,prefix in [(deployment,'deployment'),(owner,'owner')]:
            for period in ['daily','monthly']:
                original=dict(obj.metadata_json)
                obj.metadata_json={**original,f'{period}_usd':'0.10'}
                with pytest.raises(BudgetDenied,match=f'{prefix}_{period}_budget_exhausted'):
                    _policy_checks(session,run,Decimal('0.11'))
                obj.metadata_json=original
        assert _policy_checks(session,run,Decimal('0'))==(deployment,owner)


@pytest.mark.parametrize('case,code',[('durability','durability_compromised'),('paused','dispatch_paused'),('epoch','execution_epoch_changed')])
def test_policy_locks_precede_spending_authority(factory,monkeypatch,case,code):
    run_id,_=running(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id)
        dep,_=lock_policies(session,run.owner_id)
        if case=='durability': monkeypatch.setattr('round_robin.operations.durability.marker_pending',lambda **kw:True)
        if case=='paused': dep.metadata_json={**dep.metadata_json,'dispatch_locked':True}
        if case=='epoch': dep.metadata_json={**dep.metadata_json,'deployment_epoch':run.deployment_epoch+1}
        with pytest.raises(BudgetDenied,match=code): _policy_checks(session,run,Decimal('0'))


def test_settlement_is_idempotent_but_conflicting_receipts_cannot_rewrite_history(factory):
    run_id,step_id=running(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);step=session.get(Step,step_id)
        with pytest.raises(BudgetDenied,match='unknown_operation'): reconcile_operation(session,'missing','0')
        operation=authorize_operation(session,run,'receipt','model','0',step=step)
        with pytest.raises(BudgetDenied,match='invalid_usage_certainty'): reconcile_operation(session,'receipt','0','guessed')
        reconcile_operation(session,'receipt',None,'reported')
        assert operation.status=='unknown'
        reconcile_operation(session,'receipt','0','reported')
        assert reconcile_operation(session,'receipt',None,'unknown') is operation
        assert reconcile_operation(session,'receipt','0','reported') is operation
        with pytest.raises(BudgetDenied,match='usage_reconciliation_conflict'): reconcile_operation(session,'receipt','1','reported')


@pytest.mark.parametrize('case,code',[('deleted','meeting_deleted'),('epoch','execution_epoch_changed'),('schema','schema_incompatible'),('terminal','run_terminal'),('deadline','active_deadline_exceeded')])
def test_admission_checks_reject_fenced_runs_before_work(factory,case,code):
    run_id,_=running(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id)
        if case=='deleted': session.get(Meeting,run.meeting_id).deleted_at=utcnow()
        if case=='epoch':
            dep,_=lock_policies(session,run.owner_id);dep.metadata_json={**dep.metadata_json,'deployment_epoch':run.deployment_epoch+1}
        if case=='schema': run.configuration={**run.configuration,'schema_version':'future'}
        if case=='terminal': run.status='cancel_requested'
        if case=='deadline': run.active_seconds=600
        with pytest.raises(BudgetDenied,match=code): check_eligible(session,run)


def test_admission_existing_waiting_and_resumption_tickets(factory):
    run_id=seed(factory)
    with factory.begin() as session:
        assert lock_work_run(session,'missing') is None
        run=lock_work_run(session,run_id)
        run.status='awaiting_input'
        assert admit_run(session,run) is False
        run.status='queued'
        dep,_=lock_policies(session,run.owner_id); dep.metadata_json={**dep.metadata_json,'draining':True}
        assert admit_run(session,run) is False
        dep.metadata_json={**dep.metadata_json,'draining':False}
        run.initial_admission_expires_at=utcnow()-timedelta(seconds=1)
        with pytest.raises(BudgetDenied,match='admission_expired'): admit_run(session,run)
        run.first_admitted_at=utcnow()-timedelta(hours=1)
        run.resumption_expires_at=utcnow()-timedelta(seconds=1)
        with pytest.raises(BudgetDenied,match='resumption_expired'): admit_run(session,run)


def test_circuit_failed_half_open_probe_cannot_remain_leased(factory):
    run_id,_=running(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);dep,_=lock_policies(session,run.owner_id)
        scope=_scope(run,'openai')
        dep.metadata_json={**dep.metadata_json,'circuits':{scope:{'open_until':(utcnow()-timedelta(seconds=1)).isoformat(),'probe_in_flight':False}}}
        before_attempt(session,run,'openai')
        assert dep.metadata_json['circuits'][scope]['probe_in_flight']
        record_outcome(session,run,'openai',ProviderFailure('refusal'))
        assert dep.metadata_json['circuits'][scope]['probe_in_flight'] is False
        with pytest.raises(ProviderFailure,match='circuit_open'): before_attempt(session,run,'openai')


def test_recovery_and_manual_retry_never_renew_exhausted_counters(factory):
    run_id,_=running(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);step=session.scalar(select(Step))
        failure=ProviderFailure('timeout',retryable=True,retry_after=float('nan'))
        step.infrastructure_claims=3
        assert not plan_recovery(run,step,failure).retry
        step.infrastructure_claims=1
        assert plan_recovery(run,step,failure).delay_seconds==2.5
        run.active_seconds=599
        assert not plan_recovery(run,step,failure).retry
        run.status='completed_with_limitations'
        assert retryable_steps(session,run)==[]
        run.active_seconds=0
        step.calls=24
        assert retryable_steps(session,run)==[]
        step.calls=1;step.stage='synthesis';step.status='failed';step.error_code='timeout'
        session.add(Step(id='prior_audit',run_id=run_id,stage='audit',role_slot_id='audit',provider='google',status='succeeded'))
        session.flush()
        assert retryable_steps(session,run)==[]


def test_nonfinal_lower_receipt_cannot_reduce_previously_observed_uncertain_exposure(factory):
    run_id,step_id=running(factory)
    with factory.begin() as session:
        run=lock_work_run(session,run_id);step=session.get(Step,step_id)
        operation=authorize_operation(session,run,'uncertain_high_water','model','0',step=step)
        operation.reserved_usd='0.40'
        reconcile_operation(session,operation.id,'0.90','unknown')
        reconcile_operation(session,operation.id,'0.10','unknown')
        assert Decimal(operation.settled_usd)==Decimal('0.90')
        assert operations_exposure([operation])==Decimal('0.90')
        reconcile_operation(session,operation.id,None,'unknown')
        assert operations_exposure([operation])==Decimal('0.90')
        reconcile_operation(session,operation.id,'0.10','reported')
        assert operation.status=='settled' and operations_exposure([operation])==Decimal('0.10')
