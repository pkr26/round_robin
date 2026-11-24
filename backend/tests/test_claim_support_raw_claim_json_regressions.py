from dataclasses import replace

from fastapi.testclient import TestClient
from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import Run, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import StoredClaim


def test_malformed_persisted_claim_json_has_sanitized_unavailable_response(factory, tmp_path):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        content = StoredClaim.model_validate({'id':'claim_existing', 'version':1, 'proposition':'Synthetic recommendation',
            'kind':'recommendation', 'evidence_status':'not_checked', 'time_sensitive':True,
            'verified_at':utcnow().isoformat()}).model_dump(mode='json')
        session.add(ClaimVersion(id=content['id'], version=1, run_id=run.id, meeting_id=run.meeting_id,
            owner_id=run.owner_id, step_id=None, content=content, sha256=canonical_hash(content)))
    with factory.kw['bind'].begin() as connection:
        connection.exec_driver_sql('UPDATE claim_versions SET content = ? WHERE id = ?', ('{secret invalid', 'claim_existing'))
    app = create_app(str(factory.kw['bind'].url), settings=replace(Settings(), artifact_directory=tmp_path/'private'))
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f'/api/runs/{run_id}/claims')
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'claim_unreadable'
    assert 'secret invalid' not in response.text
