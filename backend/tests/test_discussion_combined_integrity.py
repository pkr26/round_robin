import pytest
from round_robin.database.models import BudgetOperation, EvidenceRecord, ReportVersion
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.reports.read_adapters import read_runtime_report
from sqlalchemy import select

from backend.tests.test_discussion_integrity import corrupt

pytest_plugins = ('backend.tests.test_artifacts_api',)


@pytest.mark.parametrize('cancelled', [False, True])
def test_simultaneous_corruption_preserves_exposure_and_never_claims_readiness(artifacts, cancelled):
    app, client, meeting_id, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.status = 'cancel_requested' if cancelled else 'running'
        run.input_snapshot = {**run.input_snapshot, 'horizon_months':'PRIVATE_INPUT_SENTINEL'}
        corrupt(run)
        session.add(BudgetOperation(id='unknown_review', run_id=run.id, owner_id=run.owner_id, kind='public_search', status='unknown', reserved_usd='0.42', metadata_json={'model_call':False}))
        session.add(EvidenceRecord(id='evidence_bad', run_id=run_id, meeting_id=meeting_id, owner_id=run.owner_id, content={'private':'PRIVATE_EVIDENCE_SENTINEL'}, sha256='0'*64))
        session.flush()
        advance_run(session, run)
    with app.state.session_factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id).order_by(ReportVersion.version.desc()).limit(1))
        report = read_runtime_report(session,row)
        assert report['readiness'] is None and report['recommendation'] is None and report['comparison'] == []
        assert report['assessment']['unresolved_critical_defect'] is True
        assert report['usage']['unknown_operation_ids'] == ['unknown_review']
        assert report['usage']['conservative_exposure_usd'] == '0.42'
        assert 'discussion ledger cannot be validated' in str(report['readiness_unavailable_reason'])
        assert 'Stored evidence could not be validated' in str(report['main_reasons'])
        assert session.get(EvidenceRecord, 'evidence_bad') is not None
    response=client.get(f'/api/runs/{run_id}/snapshot')
    assert response.status_code == 200, response.text
    body=response.json()
    assert body['brief'] is None and body['evidence'] is None and not body['retry_eligible']
    assert body['run']['status'] == ('cancelled' if cancelled else 'failed')
    if cancelled: assert body['run']['stop_reason'] == 'user_cancellation'
    assert 'PRIVATE_INPUT_SENTINEL' not in response.text and 'PRIVATE_EVIDENCE_SENTINEL' not in response.text
