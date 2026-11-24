from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


@pytest.mark.parametrize('damage', ['ledger_owner', 'ledger_run', 'ledger_kind', 'ledger_bound', 'attempt_provider', 'attempt_bound', 'attempt_epoch', 'attempt_generation', 'attempt_fence', 'attempt_lease', 'attempt_request', 'attempt_request_bool_as_int', 'attempt_configuration_int_as_float'])
def test_misbound_ledger_is_not_settled_after_text_send(factory, monkeypatch, damage):
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
    class Provider(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal('0.40')
        def generate(self, request):
            result = super().generate(request)
            with factory.begin() as session:
                attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
                ledger = session.get(BudgetOperation, attempt.id)
                if damage == 'ledger_owner': ledger.owner_id = 'foreign_owner'
                elif damage == 'ledger_run': ledger.run_id = 'foreign_run'
                elif damage == 'ledger_kind': ledger.kind = 'foreign_kind'
                elif damage == 'ledger_bound': ledger.reserved_usd = '0.01'
                elif damage == 'attempt_provider': attempt.provider = 'foreign_provider'
                elif damage == 'attempt_bound': attempt.reserved_usd = '0.01'
                elif damage == 'attempt_epoch': attempt.authorization_epoch += 1
                elif damage == 'attempt_generation': attempt.recovery_generation += 1
                elif damage == 'attempt_fence': attempt.configuration = {**attempt.configuration, 'execution_fence': 999}
                elif damage == 'attempt_lease': attempt.lease_owner = 'foreign_lease'
                elif damage == 'attempt_request': attempt.request_json = {**attempt.request_json, 'stage': 'foreign_stage'}
                elif damage == 'attempt_configuration_int_as_float':
                    attempt.configuration = {**attempt.configuration, 'execution_fence': float(attempt.configuration['execution_fence'])}
                    flag_modified(attempt, 'configuration')
                elif damage == 'attempt_request_bool_as_int':
                    attempt.request_json = {**attempt.request_json, 'repair': 0}
                    flag_modified(attempt, 'request_json')
            return replace(result, cost_usd=Decimal('0.75'), simulated=False)
    provider = Provider('openai')
    result = execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
    assert provider.calls == 1
    assert result['status'] == 'persistence_unconfirmed'
    with factory() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert ledger.status == 'reserved' and ledger.settled_usd is None
        assert attempt.completed_at is None and attempt.response_json is None
