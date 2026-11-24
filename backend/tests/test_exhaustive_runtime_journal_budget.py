"""Conservative journal accounting under malformed authority and duplicate writes."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import runpy
import sys

import pytest

from experiments.adapters import ContractError, Failure, prepare_packet, retry_is_eligible, validate_contribution
from experiments.journal import Journal, JournalError
from experiments.runner import JournalBudget, OfflineRunner, Policy, PolicyError, identity, money, main


@pytest.mark.parametrize('value',['NaN','-1','no-number',True,None])
def test_runner_money_validation_never_grants_ambiguous_spending(value):
    with pytest.raises(PolicyError):money(value)


@pytest.mark.parametrize('value',['',None,'has spaces','a'*129,'../../secret'])
def test_runner_command_identity_is_noncontent_and_bounded(value):
    with pytest.raises(PolicyError):identity(value)


@pytest.mark.parametrize('kwargs',[{'batch_requests':True},{'finish_role_seconds':0},{'run_requests':1},{'run_requests':25},{'active_seconds':601}])
def test_journal_policy_cannot_relax_hard_run_bounds(kwargs):
    with pytest.raises(PolicyError):Policy(**kwargs)


def budget(journal,policy=None):
    ledger=JournalBudget(journal,policy)
    ledger.open_batch('batch');ledger.open_run('batch','run','owner')
    return ledger


@pytest.mark.parametrize('kind',['duplicate_batch','duplicate_run','duplicate_authorization','invalid_finish','duplicate_settlement','duplicate_adjustment','unknown_event','missing_field'])
def test_replayed_semantically_invalid_history_fails_closed(tmp_path,kind):
    with Journal(tmp_path/'authority') as journal:
        ledger=budget(journal)
        ledger.authorize(run_id='run',step_id='step',attempt_id='attempt',bound_usd='0.10',finish_role='synthesis')
        ledger.settle('attempt',reported_usd='0.10',trusted_final=True,outcome='succeeded')
        ledger.reconcile('attempt','adjustment',final_usd='0.10')
        types={'duplicate_batch':'batch_opened','duplicate_run':'run_opened','duplicate_authorization':'authorized','invalid_finish':'authorized','duplicate_settlement':'settled','duplicate_adjustment':'reconciled'}
        event=deepcopy(next(row for row in journal.events if row['type']==types[kind])) if kind in types else {'type':'unknown'} if kind=='unknown_event' else {}
        if kind=='invalid_finish':event.update(attempt_id='second',bound_usd='0.30')
        journal.events.append(event)
        with pytest.raises(JournalError,match='invalid journal'):ledger._state()


def test_clock_policy_binding_and_owned_identity_cannot_be_reinterpreted(tmp_path):
    with Journal(tmp_path/'authority') as journal:
        ledger=budget(journal)
        ledger.open_batch('other')
        ledger.open_run('batch','run','owner')
        with pytest.raises(PolicyError,match='identity conflict'):ledger.open_run('other','run','owner')
        with pytest.raises(PolicyError,match='unknown batch'):ledger.open_run('missing','another')
        with pytest.raises(PolicyError,match='binding changed'):ledger.open_batch('batch',metadata={'new':'authority'})
        with pytest.raises(PolicyError,match='policy mismatch'):JournalBudget(journal,Policy(batch_usd='1'))
        ledger.clock=lambda:datetime(2026,1,1)
        with pytest.raises(PolicyError,match='timezone-aware'):ledger._now()
        ledger.clock=lambda:datetime(2000,1,1,tzinfo=timezone.utc)
        with pytest.raises(PolicyError,match='clock_regression'):ledger._now()


@pytest.mark.parametrize('kind,code',[('unknown_run','unknown run'),('timeout','invalid timeout'),('role','invalid final role'),('locked','spending_locked'),('duplicate','already_authorized'),('step_count','step_call_limit'),('run_count','run_request_limit'),('batch_count','batch_request_limit'),('finish_role','already_used_or_underfunded')])
def test_journal_authorization_rejects_invalid_or_exhausted_dimension(tmp_path,kind,code):
    policy=Policy(batch_requests=2 if kind=='batch_count' else 20,run_requests=2 if kind=='run_count' else 24)
    with Journal(tmp_path/'authority') as journal:
        ledger=budget(journal,policy)
        args={'run_id':'run','step_id':'step','attempt_id':'next','bound_usd':'0.01'}
        if kind=='unknown_run':args['run_id']='missing'
        if kind=='timeout':args['timeout_seconds']=False
        if kind=='role':args['finish_role']='proposal'
        if kind=='locked':ledger.set_spending_lock(True)
        if kind in {'duplicate','step_count','run_count','batch_count','finish_role'}:
            ledger.authorize(run_id='run',step_id='step',attempt_id='first',bound_usd='0.01',finish_role='synthesis')
        if kind in {'step_count','run_count','batch_count'}:
            ledger.authorize(run_id='run',step_id='step' if kind=='step_count' else 'different',attempt_id='second',bound_usd='0.01',finish_role='audit')
        if kind=='duplicate':args['attempt_id']='first'
        if kind=='finish_role':args['finish_role']='synthesis'
        count=len(ledger._state()['attempts'])
        with pytest.raises(PolicyError,match=code):ledger.authorize(**args)
        assert len(ledger._state()['attempts'])==count


def test_run_opening_preserves_batch_finish_slots_and_spending_kill_switch(tmp_path):
    with Journal(tmp_path/'authority') as journal:
        ledger=budget(journal,Policy(batch_requests=2))
        with pytest.raises(PolicyError,match='batch_finish_call_reserve'):ledger.open_run('batch','second')
        ledger.set_spending_lock(True)
        with pytest.raises(PolicyError,match='spending_locked'):ledger.open_run('batch','second')


def test_settlement_reconciliation_and_closure_are_evidence_idempotent(tmp_path):
    with Journal(tmp_path/'authority') as journal:
        ledger=budget(journal)
        with pytest.raises(PolicyError,match='unknown attempt'):ledger.settle('missing',reported_usd='0',trusted_final=True,outcome='succeeded')
        with pytest.raises(PolicyError,match='unknown attempt'):ledger.reconcile('missing','adj',final_usd='0')
        with pytest.raises(PolicyError,match='unknown attempt'):ledger.book_conservative('missing','0')
        with pytest.raises(PolicyError,match='unknown run'):ledger.close_run('missing')
        ledger.authorize(run_id='run',step_id='step',attempt_id='attempt',bound_usd='0.10')
        for values in [{'outcome':'forged','reported_usd':'0','trusted_final':True},{'outcome':'unknown','reported_usd':None,'trusted_final':True}]:
            with pytest.raises(PolicyError):ledger.settle('attempt',**values)
        ledger.settle('attempt',reported_usd='0.1',trusted_final=True,outcome='succeeded')
        n=len(journal.events)
        ledger.settle('attempt',reported_usd='0.1',trusted_final=True,outcome='succeeded')
        assert len(journal.events)==n
        with pytest.raises(PolicyError,match='settlement conflict'):ledger.settle('attempt',reported_usd='0.2',trusted_final=True,outcome='succeeded')
        ledger.reconcile('attempt','adjusted',final_usd='0.2')
        n=len(journal.events);ledger.reconcile('attempt','adjusted',final_usd='0.2');assert len(journal.events)==n
        with pytest.raises(PolicyError,match='identity conflict'):ledger.reconcile('attempt','adjusted',final_usd='0.3')
        with pytest.raises(PolicyError,match='invalid spending lock'):ledger.set_spending_lock('no')
        ledger.close_run('run');n=len(journal.events);ledger.close_run('run');assert len(journal.events)==n


def test_adapter_packet_and_output_bad_shapes_are_controlled_failures():
    with pytest.raises(ContractError,match='invalid values'):prepare_packet({'value':float('nan')})
    for contribution in [{'simulated':True,'summary':'  ','reference_ids':[],'confidence':'0.5'},{'simulated':True,'summary':'A','reference_ids':['foreign'],'confidence':'0.5'}]:
        with pytest.raises(ContractError):validate_contribution(contribution,allowed_ids=set())
    assert not retry_is_eligible(Failure('timeout',True,True),calls_used=1,remaining_seconds=60,retry_cost=Decimal('NaN'),available_money=Decimal('1'))


def test_runner_cli_safe_failure_and_module_entry_do_not_touch_providers(tmp_path,capsys,monkeypatch):
    assert main(['--journal-dir',str(tmp_path/'authority'),'--batch-id','invalid id'])==2
    assert 'invalid id' not in capsys.readouterr().err
    monkeypatch.setattr(sys,'argv',['runner','--journal-dir',str(tmp_path/'authority'),'--batch-id','batch_cli','--simulate'])
    with pytest.raises(SystemExit) as error:runpy.run_module('experiments.runner',run_name='__main__')
    assert error.value.code==0
    data=capsys.readouterr().out
    assert '"live_provider_calls": 0' in data
