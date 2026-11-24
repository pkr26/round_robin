from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from round_robin.database.models import Attempt, BudgetOperation, Run, Step
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step

from backend.tests.test_text_finish_pre_send_regressions import live_run


@pytest.mark.parametrize('damage', ['underfunded', 'foreign_owner', 'foreign_run', 'model_call'])
@pytest.mark.parametrize('reported', [False, True])
def test_changed_hold_after_send_fences_without_losing_usage(factory, monkeypatch, damage, reported):
    run_id, step_id = live_run(factory, monkeypatch)
    class Provider(SimulatedProvider):
        def conservative_bound(self, request):
            return Decimal('0.10')
        def generate(self, request):
            result = super().generate(request)
            with factory.begin() as session:
                hold = session.get(BudgetOperation, f'finish_{run_id}')
                if damage == 'underfunded': hold.reserved_usd = '0.01'
                elif damage == 'foreign_owner': hold.owner_id = 'foreign_owner'
                elif damage == 'foreign_run': hold.run_id = 'foreign_run'
                elif damage == 'model_call': hold.metadata_json = {**hold.metadata_json, 'model_call': True}
            if not reported:
                raise ProviderFailure('timeout', may_have_sent=True)
            return replace(result, cost_usd=Decimal('0.75'), simulated=False)
    provider = Provider('openai')
    result = execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
    assert result['status'] == 'late_result_fenced' and provider.calls == 1
    with factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        assert run.status == 'failed' and run.configuration['ledger_integrity_failed'] is True
        assert run.report_version == 0 and session.get(Step, step_id).result_json is None
        assert (ledger.status, ledger.settled_usd) == (('settled', '0.75') if reported else ('unknown', None))
        assert hold.status == 'held' and hold.reserved_usd == ('0.01' if damage == 'underfunded' else '0.40')
