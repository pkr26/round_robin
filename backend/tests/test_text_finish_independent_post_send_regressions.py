from dataclasses import replace
from decimal import Decimal

from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import Attempt, BudgetOperation, Run, Step
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


def test_underfunded_finish_after_send_preserves_known_receipt_and_fails_closed(factory, monkeypatch):
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
        def conservative_bound(self, request): return Decimal('0.40')
        def generate(self, request):
            result = super().generate(request)
            with factory.begin() as session:
                session.get(BudgetOperation, f'finish_{run_id}').reserved_usd = '0.01'
            return replace(result, cost_usd=Decimal('0.75'), simulated=False)
    provider = Provider('openai')
    execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
    assert provider.calls == 1
    with factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert ledger.status == 'settled' and ledger.settled_usd == '0.75'
        assert attempt.response_json.get('content_discarded') is True
        assert run.status == 'failed' and run.configuration.get('ledger_integrity_failed') is True
        assert session.get(BudgetOperation, f'finish_{run_id}').reserved_usd == '0.01'
