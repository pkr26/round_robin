from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Run, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import authorize_operation, checked_finish_hold
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


def live_run(factory, monkeypatch):
    monkeypatch.setattr('round_robin.orchestration.configuration.verify_frozen_configuration', lambda _: None)
    config = run_configuration(Settings(), 'simulated')
    config.update(mode='live', funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, 'owner_test'):
            policy.spending_locked, policy.limit_usd = False, '10'
            policy.metadata_json = {'daily_usd': '10', 'monthly_usd': '50'}
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    return run_id, step_id


@pytest.mark.parametrize('damage', ['owner', 'run', 'kind', 'status', 'model_call', 'booking', 'settled', 'duplicate_roles', 'wrong_role', 'missing_initial', 'wrong_initial', 'underfunded'])
def test_misbound_or_ambiguous_active_hold_blocks_before_provider(factory, monkeypatch, damage):
    run_id, step_id = live_run(factory, monkeypatch)
    with factory.begin() as session:
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        if damage == 'owner': hold.owner_id = 'foreign_owner'
        elif damage == 'run': hold.run_id = 'foreign_run'
        elif damage == 'kind': hold.kind = 'model'
        elif damage == 'status': hold.status = 'released'
        elif damage == 'settled': hold.settled_usd = '0.01'
        elif damage == 'underfunded': hold.reserved_usd = '0.01'
        else:
            metadata = dict(hold.metadata_json)
            if damage == 'model_call': metadata['model_call'] = True
            elif damage == 'booking': metadata['conservative_booking_usd'] = '0.10'
            elif damage == 'duplicate_roles': metadata['roles_remaining'] = ['audit', 'audit']
            elif damage == 'wrong_role': metadata['roles_remaining'] = ['audit', 'foreign']
            elif damage == 'missing_initial': del metadata['initial_reserved_usd']
            elif damage == 'wrong_initial': metadata['initial_reserved_usd'] = '0.02'
            hold.metadata_json = metadata
        before = (hold.owner_id, hold.run_id, hold.kind, hold.status, hold.reserved_usd, hold.settled_usd, deepcopy(hold.metadata_json))
    provider = SimulatedProvider('openai')
    result = execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
    assert provider.calls == 0 and result['status'] == 'policy_blocked'
    with factory() as session:
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        assert (hold.owner_id, hold.run_id, hold.kind, hold.status, hold.reserved_usd, hold.settled_usd, hold.metadata_json) == before
        assert not list(session.scalars(select(Attempt).where(Attempt.run_id == run_id)))
        run = session.get(Run, run_id)
        assert run.status == 'failed' and run.configuration['ledger_integrity_failed'] is True
        assert run.report_version == 0


def test_cheaper_finish_transfers_leave_legitimate_surplus_and_release_it(factory, monkeypatch):
    run_id, step_id = live_run(factory, monkeypatch)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        step = session.get(Step, step_id)
        step.stage, step.role_slot_id = 'synthesis', 'synthesis'
        authorize_operation(session, run, 'first_synthesis', 'model', '0.05', step=step, finish_role='synthesis')
        hold = checked_finish_hold(session, run, lock=True)
        assert Decimal(hold.reserved_usd) == Decimal('0.35') and hold.metadata_json['roles_remaining'] == ['audit']
        step.stage, step.role_slot_id = 'audit', 'audit'
        authorize_operation(session, run, 'next_audit', 'model', '0.05', step=step, finish_role='audit')
        hold = checked_finish_hold(session, run, lock=True)
        assert Decimal(hold.reserved_usd) == Decimal('0.30') and hold.metadata_json['roles_remaining'] == []
        from round_robin.orchestration.budget import release_unused_finish
        assert release_unused_finish(session, run) is True
        assert hold.status == 'released' and hold.reserved_usd == '0'
