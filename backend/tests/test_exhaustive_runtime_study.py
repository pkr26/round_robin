"""Independent attacks on study authorization, immutable receipts and output identity."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.tests.test_study_runner import funded_study, mock_registry
from experiments.journal import Journal
from experiments.runner import PolicyError
from experiments import study_runner as study
from experiments.study_runner import CheckedSnapshot, FrozenStudyManifest, StudyCase, StudyApproval, StudyBlocked, FundedStudyRunner, StudyLedger, private_write, load_study_approval, set_spending_lock
from round_robin.database.models import utcnow
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider


@pytest.mark.parametrize('case',['snapshot_hash','unconfirmed','scope','distinct_tasks','future_snapshot','repeat_ids','funding','aggregate','expiry'])
def test_authorization_models_reject_invalid_original_scope(funded_study,case):
    _,(approval,manifest,_)=funded_study
    body=manifest.model_dump(mode='json')
    if case=='snapshot_hash':body['tasks'][0]['checked_snapshots'][0]['sha256']='a'*64
    if case=='unconfirmed':body['tasks'][0]['brief']['horizon_months']=None
    if case=='scope':body['tasks'][0]['brief']['options'][0]['cost_components'][0]['source_ref']='evidence_foreign'
    if case=='distinct_tasks':body['tasks'][1]['task_id']=body['tasks'][0]['task_id']
    if case=='future_snapshot':body['tasks'][0]['checked_snapshots'][0]['checked_at']=(utcnow()+timedelta(minutes=2)).isoformat()
    if case=='repeat_ids':body['repeated_task_ids'][0]='UNREGISTERED'
    model=FrozenStudyManifest
    if case in {'funding','aggregate','expiry'}:
        body=approval.model_dump(mode='json');model=StudyApproval
        if case=='funding':body['synthesizer']=body['auditor']
        if case=='aggregate':body['owner_daily_usd']='0'
        if case=='expiry':body['expires_at']=body['authorized_at']
    with pytest.raises(ValidationError):model.model_validate(body)


def mutate_record(approval_path,key,change):
    body=json.loads(approval_path.read_text());path=Path(body[key]['path'])
    value=json.loads(path.read_text());value=change(value)
    path.write_text(json.dumps(value));body[key]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    approval_path.write_text(json.dumps(body))


@pytest.mark.parametrize('case',['future_approval','future_manifest','bad_discovery','incomplete_discovery','baseline_shape','baseline_decision','review_shape','review_plan','finish_reserve'])
def test_linked_approval_artifacts_are_revalidated_before_funding(funded_study,case):
    path,(approval,manifest,config)=funded_study
    body=json.loads(path.read_text())
    if case=='future_approval':body['authorized_at']=(utcnow()+timedelta(minutes=1)).isoformat();path.write_text(json.dumps(body))
    if case=='future_manifest':mutate_record(path,'manifest',lambda v:{**v,'frozen_at':(utcnow()+timedelta(seconds=1)).isoformat()})
    if case=='bad_discovery':mutate_record(path,'discovery_record',lambda v:[])
    if case=='incomplete_discovery':mutate_record(path,'discovery_record',lambda v:{**v,'interviews':v['interviews'][:4]})
    if case=='baseline_shape':mutate_record(path,'baseline_selection_record',lambda v:[])
    if case=='baseline_decision':mutate_record(path,'baseline_selection_record',lambda v:{**v,'self_check_enabled':False})
    if case=='review_shape':mutate_record(path,'reviewer_plan_record',lambda v:[])
    if case=='review_plan':mutate_record(path,'reviewer_plan_record',lambda v:{**v,'reviewer_ids':['only_one']})
    if case=='finish_reserve':body['finish_role_usd']='0.0001';path.write_text(json.dumps(body))
    with pytest.raises((StudyBlocked,ValidationError)):load_study_approval(path)


def test_authorization_file_readers_and_immutable_writer_reject_links_and_overflow(tmp_path):
    target=tmp_path/'private.json';private_write(target,{'safe':'data'})
    link=tmp_path/'linked.json';link.symlink_to(target)
    with pytest.raises(StudyBlocked,match='symlink'):private_write(link,{'safe':'data'})
    for path in [link,tmp_path/'missing']:
        with pytest.raises(StudyBlocked):study._read_private_json(path)
        with pytest.raises(StudyBlocked):study._checked_file(SimpleNamespace(path=path,sha256='a'*64))
    with pytest.raises(StudyBlocked):study._read_private_json(target,maximum=1)
    with pytest.raises(StudyBlocked,match='conflict'):private_write(target,{'changed':True})
    assert json.loads(target.read_text())=={'safe':'data'}


@pytest.mark.parametrize('target',['policy','lock','fifo','pending','identity','boolean'])
def test_spending_policy_control_cannot_follow_links_or_ambiguous_authority(funded_study,tmp_path,target):
    _,(approval,_,_)=funded_study
    policy=approval.spending_policy_file
    lock=policy.with_suffix(policy.suffix+'.lock')
    original=policy.read_bytes()
    if target=='policy':
        saved=tmp_path/'outside_policy';saved.write_bytes(original);policy.unlink();policy.symlink_to(saved)
    if target=='lock':lock.unlink();lock.symlink_to(policy)
    if target=='fifo':lock.unlink();os.mkfifo(lock)
    if target=='pending':policy.with_suffix(policy.suffix+'.pending').write_text('interrupted')
    candidate=approval.model_copy(update={'owner_id':'different-owner'}) if target=='identity' else approval
    with pytest.raises(StudyBlocked):set_spending_lock(candidate,'false' if target=='boolean' else True)
    assert policy.read_bytes()==original


def runner_context(funded_study):
    _,(approval,manifest,config)=funded_study
    registry,requests=mock_registry(config)
    return approval,manifest,config,registry,requests


def test_study_ledger_requires_current_policy_lock_and_checks_lowered_money(funded_study):
    approval,manifest,config,registry,requests=runner_context(funded_study)
    with Journal(approval.journal_directory,mode='explicit_live_study') as journal:
        runner=FundedStudyRunner(journal,approval,manifest,config,provider_registry=registry)
        with pytest.raises(StudyBlocked,match='lock_required'):
            runner.ledger._check_money(runner.ledger._state(),approval.batch_id,approval.owner_id,'trial_new',Decimal('0'),'2026-01-01T00:00:00+00:00')
        set_spending_lock(approval.model_copy(update={'owner_daily_usd':'0.10'}),False)
        with pytest.raises(PolicyError,match='current_operator_currency'):
            runner.ledger.open_run(approval.batch_id,'trial_new',approval.owner_id)
        assert not requests and not runner.ledger._state()['runs']


def test_study_ledger_gates_expired_and_lowered_request_authority(funded_study):
    approval,manifest,config,registry,requests=runner_context(funded_study)
    with Journal(approval.journal_directory,mode='explicit_live_study') as journal:
        runner=FundedStudyRunner(journal,approval,manifest,config,provider_registry=registry)
        set_spending_lock(approval.model_copy(update={'batch_requests':1}),False)
        with pytest.raises(PolicyError,match='current_operator_request_limit'):
            runner.ledger.open_run(approval.batch_id,'new',approval.owner_id)
        runner.ledger.approval=approval.model_copy(update={'expires_at':utcnow()-timedelta(seconds=1)})
        with pytest.raises(StudyBlocked,match='approval_expired'):
            with runner.ledger.gate():pass
        assert runner.ledger.current_policy is None and requests==[]


@pytest.mark.parametrize('case',['expired_approval','changed_config','expired_config','clock_regression','missing_credentials','wrong_journal'])
def test_study_executor_rechecks_frozen_authority_and_never_reports_negative_elapsed(funded_study,monkeypatch,case):
    approval,manifest,config,registry,requests=runner_context(funded_study)
    if case=='wrong_journal':
        with Journal(approval.journal_directory) as journal:
            with pytest.raises(StudyBlocked,match='profile_or_authority'):
                FundedStudyRunner(journal,approval,manifest,config,provider_registry=registry)
        return
    with Journal(approval.journal_directory,mode='explicit_live_study') as journal:
        if case=='missing_credentials':
            with pytest.raises(StudyBlocked,match='credentials_required'):
                FundedStudyRunner(journal,approval,manifest,config)
            return
        runner=FundedStudyRunner(journal,approval,manifest,config,provider_registry=registry)
        if case=='expired_approval':runner.approval=approval.model_copy(update={'expires_at':utcnow()-timedelta(seconds=1)})
        if case=='changed_config':runner.configuration['models']['openai']='unapproved-model'
        if case=='expired_config':
            original=study._checked_file
            def checked(record):
                value=original(record)
                return {**value,'expires_at':(utcnow()-timedelta(seconds=1)).isoformat()} if record==approval.provider_configuration else value
            monkeypatch.setattr(study,'_checked_file',checked)
        if case=='clock_regression':
            runner.ledger.open_run(approval.batch_id,'trial_clock',approval.owner_id)
            monkeypatch.setattr(study,'utcnow',lambda:approval.authorized_at-timedelta(hours=1))
            with pytest.raises(StudyBlocked,match='clock_regression'):runner._elapsed('trial_clock')
        else:
            with pytest.raises(StudyBlocked):runner._authority()
        assert requests==[]


@pytest.mark.parametrize('kind',['refusal','before_send','retry_nan','retry_too_late','fallback','simulated','policy_timeout'])
def test_study_call_failures_preserve_receipts_and_original_two_call_limit(funded_study,monkeypatch,kind):
    approval,manifest,config,registry,requests=runner_context(funded_study)
    with Journal(approval.journal_directory,mode='explicit_live_study') as journal:
        sleeps=[]
        runner=FundedStudyRunner(journal,approval,manifest,config,provider_registry=registry,sleeper=sleeps.append)
        trial=next(item for item in runner.plan if item.workflow=='single_provider')
        run_id=runner._run_id(trial);runner.ledger.open_run(approval.batch_id,run_id,approval.owner_id)
        if kind=='policy_timeout':monkeypatch.setattr(runner,'_elapsed',lambda _:600)
        if kind=='simulated':runner.providers['openai']=SimulatedProvider('openai')
        elif kind!='policy_timeout':
            calls=[]
            def generate(request):
                calls.append(request)
                if kind=='retry_too_late':monkeypatch.setattr(runner,'_elapsed',lambda _:590)
                raise ProviderFailure('refusal' if kind=='refusal' else 'authentication' if kind=='fallback' else 'timeout',retryable=kind in {'retry_nan','retry_too_late','before_send'},retry_after=float('nan') if kind=='retry_nan' else None,may_have_sent=kind!='before_send',provider_request_id='owned_receipt')
            monkeypatch.setattr(runner.providers['openai'],'generate',generate)
        result=runner._call(trial,'proposal_manual','proposal','openai',[],fallbacks=['anthropic'] if kind=='fallback' else ())
        attempts=list(runner.ledger._state()['attempts'].values())
        assert len(attempts)<=2
        if kind=='fallback':assert result['output'] and result['provider']=='anthropic' and len(attempts)==2
        else:assert result['output'] is None
        if kind=='refusal':assert len(attempts)==1 and result['failures'][0]['error_code']=='refusal'
        if kind=='before_send':assert all(row['settlement']['trusted_final'] and Decimal(row['settlement']['reported_usd'])==0 for row in attempts)
        if kind=='retry_nan':assert sleeps==[2.5] and len(attempts)==2
        if kind=='policy_timeout':assert not attempts
        if kind=='simulated':assert attempts[0]['settlement'] is None


def test_study_cached_outcome_cannot_substitute_missing_or_wrong_scope_content(funded_study):
    approval,manifest,config,registry,requests=runner_context(funded_study)
    with Journal(approval.journal_directory,mode='explicit_live_study') as journal:
        runner=FundedStudyRunner(journal,approval,manifest,config,provider_registry=registry)
        trial=next(item for item in runner.plan if item.workflow=='single_provider')
        path=runner._trial_directory(trial)/'outcome.json'
        private_write(path,{'forged':True})
        with pytest.raises(StudyBlocked,match='uncommitted_cached'):
            runner.execute_trial(trial)
        path.unlink();result=runner.execute_trial(trial);sends=len(requests)
        # A valid hash is insufficient if persisted outcome scope is wrong.
        result['input_hash']='a'*64;path.write_text(json.dumps(result))
        terminal=next(event for event in reversed(journal.events) if event['type']=='run_closed')
        terminal['metadata']['outcome_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with pytest.raises(StudyBlocked,match='cached_trial_binding'):
            runner.execute_trial(trial)
        path.unlink()
        with pytest.raises(StudyBlocked,match='output_missing_never_repeat'):
            runner.execute_trial(trial)
        assert len(requests)==sends


@pytest.mark.parametrize('kind',['no_proposals','no_criticism','no_synthesis'])
def test_study_output_is_honest_under_partial_or_uncontested_work(funded_study,monkeypatch,kind):
    approval,manifest,config,registry,requests=runner_context(funded_study)
    for adapter in registry.values():
        original=adapter.generate
        def generate(request,original=original):
            if kind=='no_proposals' and request.stage=='proposal' or kind=='no_synthesis' and request.stage=='synthesis':raise ProviderFailure('refusal')
            result=original(request)
            if kind=='no_criticism':result=replace(result,output=result.output.model_copy(update={'dissent':[]}))
            return result
        monkeypatch.setattr(adapter,'generate',generate)
    with Journal(approval.journal_directory,mode='explicit_live_study') as journal:
        runner=FundedStudyRunner(journal,approval,manifest,config,provider_registry=registry)
        trial=next(item for item in runner.plan if item.workflow=='bounded_debate')
        result=runner.execute_trial(trial)
        if kind=='no_proposals':assert result['status']=='failed' and not result['mandatory_work_complete']
        if kind=='no_synthesis':assert result['status']=='limited' and not result['mandatory_work_complete']
        if kind=='no_criticism':assert result['status']=='completed' and all(row['stage']!='revision' for row in result['stage_records'])
        assert result['phase_passed'] is False


def test_study_cli_successful_paths_use_explicit_mocked_funding_only(funded_study,tmp_path,monkeypatch,capsys):
    path,(approval,manifest,config)=funded_study
    assert study.main(['--schemas',str(tmp_path/'schemas.json')])==0
    assert study.main(['--approval-file',str(path),'--set-spending-lock','locked'])==0
    assert study.main(['--approval-file',str(path),'--set-spending-lock','unlocked'])==0
    assert study.main(['--approval-file',str(path)])==2
    class Runner:
        def __init__(self,*args,**kw):pass
        def run(self):return {'blocked':None,'phase_passed':False,'test_mock':True}
    monkeypatch.setattr(study,'FundedStudyRunner',Runner)
    assert study.main(['--approval-file',str(path),'--live'])==0
    assert 'test_mock' in capsys.readouterr().out
