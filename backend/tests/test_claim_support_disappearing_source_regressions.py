from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import delete

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import EvidenceRecord, Run, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research import reuse
from round_robin.research.reuse import StoredClaim
from backend.tests.test_claim_support_roots_regressions import add_source


def test_evidence_removed_between_root_and_status_read_does_not_ground_claim(factory, tmp_path, monkeypatch):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        add_source(session, run, 'evidence_source', 'supported')
        raw = {'id': 'claim_existing', 'version': 1, 'predecessor_id': None, 'proposition': 'A supported synthetic claim.',
               'kind': 'fact', 'evidence_status': 'supported', 'reference_ids': ['evidence_source'],
               'time_sensitive': True, 'verified_at': utcnow().isoformat()}
        content = StoredClaim.model_validate(raw).model_dump(mode='json')
        session.add(ClaimVersion(id=content['id'], version=1, run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id,
                                 step_id=None, content=content, sha256=canonical_hash(content)))
    original = reuse._reference_statuses
    def remove_before_status(session, run, *args, **kwargs):
        session.execute(delete(EvidenceRecord).where(EvidenceRecord.id == 'evidence_source'))
        return original(session, run, *args, **kwargs)
    monkeypatch.setattr(reuse, '_reference_statuses', remove_before_status)
    app = create_app(str(factory.kw['bind'].url), settings=replace(Settings(), artifact_directory=tmp_path / 'private'))
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app) as client:
        response = client.get(f'/api/runs/{run_id}/claims')
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'claim_unreadable'
