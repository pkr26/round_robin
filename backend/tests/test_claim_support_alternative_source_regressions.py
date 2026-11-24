from dataclasses import replace

from fastapi.testclient import TestClient
from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import EvidenceRecord, Run, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import StoredClaim
from backend.tests.test_claim_support_roots_regressions import add_source


def test_alternative_valid_basis_and_unrelated_bad_evidence_preserve_claims(factory, tmp_path):
    run_id = seed(factory)
    saved = {}
    with factory.begin() as session:
        run = session.get(Run, run_id)
        add_source(session, run, 'evidence_good', 'supported')
        add_source(session, run, 'evidence_unchecked', 'not_checked')
        add_source(session, run, 'evidence_unrelated', 'supported')
        session.get(EvidenceRecord, 'evidence_unrelated').sha256 = '0' * 64
        entries = [
            {'id': 'claim_fact', 'proposition': 'Supported by one alternative basis.', 'kind': 'fact',
             'evidence_status': 'supported', 'support_bases': [{'reference_ids': ['evidence_unchecked']}, {'reference_ids': ['evidence_good']}]},
            {'id': 'claim_recommendation', 'proposition': 'A synthetic recommendation.', 'kind': 'recommendation',
             'evidence_status': 'not_checked', 'reference_ids': []},
        ]
        for entry in entries:
            content = StoredClaim.model_validate({**entry, 'version': 1, 'time_sensitive': True, 'verified_at': utcnow().isoformat()}).model_dump(mode='json', exclude_unset=True)
            digest = canonical_hash(content)
            saved[content['id']] = (content, digest)
            session.add(ClaimVersion(id=content['id'], version=1, run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id,
                                     step_id=None, content=content, sha256=digest))
    app = create_app(str(factory.kw['bind'].url), settings=replace(Settings(), artifact_directory=tmp_path / 'private'))
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app) as client:
        response = client.get(f'/api/runs/{run_id}/claims')
    assert response.status_code == 200, response.text
    result = {item['id']: item for item in response.json()['items']}
    assert result['claim_fact']['evidence_status'] == 'supported'
    assert result['claim_recommendation']['evidence_status'] == 'not_checked'
    with factory() as session:
        for identity, (content, digest) in saved.items():
            row = session.get(ClaimVersion, identity)
            assert row.content == content and row.sha256 == digest
        assert session.get(EvidenceRecord, 'evidence_unrelated').sha256 == '0' * 64
