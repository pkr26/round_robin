"""Compound saved-data damage must not hide the accounting-unavailable state."""
import pytest
from round_robin.database.models import BudgetOperation, EvidenceRecord
from sqlalchemy import select

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import create, start
from backend.tests.test_orchestration import drain


@pytest.mark.parametrize('bad_ledger', [False, True])
def test_snapshot_fences_both_bad_evidence_and_bad_ledger(client, application, bad_ledger):
    meeting = create(client)
    started = start(client, meeting)
    assert started.status_code == 202, started.text
    run_id = started.json()['id']
    drain(application.state.session_factory)
    with application.state.session_factory.begin() as session:
        evidence = session.scalar(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id))
        assert evidence is not None
        evidence.content = {'private': 'DAMAGED_EVIDENCE_SENTINEL'}
        if bad_ledger:
            session.add(BudgetOperation(id='damaged_ledger', run_id=run_id, owner_id='owner_local', kind='model', status='unknown', reserved_usd='0.40', metadata_json=[]))
    response = client.get(f'/api/runs/{run_id}/snapshot')
    assert 'DAMAGED_EVIDENCE_SENTINEL' not in response.text
    if not bad_ledger:
        assert response.status_code == 503
        return
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['report'] is None and body['evidence'] is None
    assert body['run']['remaining_limits']['requests'] is None
    assert body['run']['remaining_limits']['requests_unavailable_reason'] == 'ledger_integrity_failed'
