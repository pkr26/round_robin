from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from backend.tests.test_runtime_regressions import live_app, start_live
from round_robin.database.models import Attempt, BudgetOperation, DeletionRecord, Meeting, Run, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


@pytest.mark.parametrize('usage', ['known', 'unknown', 'not_sent'])
@pytest.mark.parametrize('damage', [None, 'record_owner', 'record_fence', 'ledger_owner', 'ledger_kind', 'ledger_bound', 'run_status', 'unredacted_request'])
def test_deleted_run_receipt_is_scoped_and_never_unredacts(tmp_path, monkeypatch, usage, damage):
    app, _ = live_app(tmp_path, monkeypatch)
    factory = app.state.session_factory
    with TestClient(app) as client:
        meeting_id, run_id = start_live(client)
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
            step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
        class Provider(SimulatedProvider):
            def conservative_bound(self, request): return Decimal('0.40')
            def generate(self, request):
                result = super().generate(request)
                response = client.request('DELETE', f'/api/meetings/{meeting_id}', json={'command_id': str(uuid4())})
                assert response.status_code == 200, response.text
                with factory.begin() as session:
                    record = session.get(DeletionRecord, meeting_id)
                    attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
                    ledger = session.get(BudgetOperation, attempt.id)
                    if damage == 'record_owner': record.owner_id = 'foreign_owner'
                    elif damage == 'record_fence': record.execution_fence += 1
                    elif damage == 'ledger_owner': ledger.owner_id = 'foreign_owner'
                    elif damage == 'ledger_kind': ledger.kind = 'foreign_kind'
                    elif damage == 'ledger_bound': ledger.reserved_usd = '0.01'
                    elif damage == 'run_status': session.get(Run, run_id).status = 'running'
                    elif damage == 'unredacted_request': attempt.request_json = {'unexpected': 'must not overwrite'}
                if usage == 'unknown': raise ProviderFailure('timeout', may_have_sent=True)
                if usage == 'not_sent': raise ProviderFailure('offline', may_have_sent=False)
                return replace(result, cost_usd=Decimal('0.75'), simulated=False, provider_request_id='synthetic_late')
        provider = Provider('openai')
        result = execute_step(step_id, session_factory=factory, adapter_registry={'openai': provider})
        assert provider.calls == 1
    with factory() as session:
        run = session.get(Run, run_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert session.get(Meeting, meeting_id).deleted_at is not None
        assert attempt.configuration == {}
        if damage is not None:
            assert result['status'] == 'persistence_unconfirmed'
            assert ledger.status == 'reserved' and ledger.settled_usd is None
            assert attempt.completed_at is None
            assert attempt.request_json == ({'unexpected': 'must not overwrite'} if damage == 'unredacted_request' else {})
        else:
            assert result['status'] == 'late_result_fenced'
            assert run.status == 'cancelled' and run.stop_reason == 'meeting_deleted'
            assert attempt.request_json == {} and attempt.response_json in (None, {'content_discarded': True, 'usage': {}})
            assert ledger.status == ('unknown' if usage == 'unknown' else 'settled')
            assert ledger.settled_usd == {'known':'0.75', 'unknown':None, 'not_sent':'0'}[usage]
