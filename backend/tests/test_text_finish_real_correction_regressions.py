from decimal import Decimal

from backend.tests.test_exhaustive_runtime_correction import correction_candidate
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from round_robin.database.models import BudgetOperation, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import authorize_operation, checked_finish_hold, operations_exposure
from round_robin.orchestration.controller import _reserve_correction
from sqlalchemy import select


def test_real_funded_policy_preserves_surplus_across_correction_and_two_transfers(factory, monkeypatch):
    monkeypatch.setattr('round_robin.orchestration.configuration.verify_frozen_configuration', lambda _: None)
    run_id = correction_candidate(factory)
    with factory.begin() as session:
        for policy in lock_policies(session, 'owner_test'):
            policy.spending_locked, policy.limit_usd = False, '10'
            policy.metadata_json = {'daily_usd': '10', 'monthly_usd': '50'}
        run = lock_work_run(session, run_id)
        run.configuration = {**run.configuration, 'mode': 'live', 'funded': True, 'finish_policy': {**run.configuration['finish_policy'], 'reserved_usd': '0.40'}}
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        hold.reserved_usd = '0.07'
        hold.metadata_json = {**hold.metadata_json, 'initial_reserved_usd': '0.40'}
        assert _reserve_correction(session, run)
        assert Decimal(hold.reserved_usd) == Decimal('0.47')
        assert hold.metadata_json['correction_reserved_usd'] == '0.40'
        for role in ('synthesis', 'audit'):
            step = Step(id=f'correction_{role}', run_id=run_id, stage=role, round=99, role_slot_id=role, provider='openai', status='pending')
            session.add(step)
            session.flush()
            authorize_operation(session, run, f'correction_{role}_attempt', 'model', '0.10', step=step, finish_role=role)
        checked_finish_hold(session, run, lock=True)
        assert Decimal(hold.reserved_usd) == Decimal('0.27')
        assert hold.metadata_json['roles_remaining'] == []
        operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run_id)))
        assert operations_exposure(operations) == Decimal('0.47')
