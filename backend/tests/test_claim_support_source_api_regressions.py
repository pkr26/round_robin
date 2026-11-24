from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import EvidenceRecord, Run, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import StoredClaim

from backend.tests.test_claim_support_roots_regressions import add_source


@pytest.mark.parametrize('damage', ['owner', 'meeting', 'content_hash', 'status_missing', 'malformed_json'])
def test_corrupt_source_cannot_ground_saved_supported_claim(factory, tmp_path, damage):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        add_source(session, run, 'evidence_source', 'supported')
        source = session.get(EvidenceRecord, 'evidence_source')
        if damage == 'owner': source.owner_id = 'foreign_owner'
        elif damage == 'meeting': source.meeting_id = 'foreign_meeting'
        elif damage == 'content_hash': source.sha256 = '0' * 64
        elif damage == 'status_missing': source.content = {key: value for key, value in source.content.items() if key != 'verification_status'}
        raw = {'id': 'claim_existing', 'version': 1, 'predecessor_id': None, 'proposition': 'A supported synthetic claim.',
            'kind': 'fact', 'evidence_status': 'supported', 'reference_ids': ['evidence_source'], 'time_sensitive': True, 'verified_at': utcnow().isoformat()}
        content = StoredClaim.model_validate(raw).model_dump(mode='json')
        session.add(ClaimVersion(id=content['id'], version=1, run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id,
            step_id=None, content=content, sha256=canonical_hash(content)))
    if damage == 'malformed_json':
        with factory.begin() as session:
            session.execute(text("UPDATE evidence SET content='not-json' WHERE id='evidence_source'"))
    app = create_app(str(factory.kw['bind'].url), settings=replace(Settings(), artifact_directory=tmp_path / 'private'))
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app) as client:
        response = client.get(f'/api/runs/{run_id}/claims')
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'claim_unreadable'


@pytest.mark.parametrize('case,expected', [('unrelated_bad', 200), ('unrelated_bad_json', 200), ('checked_alternative', 200), ('bad_alternative', 409), ('bad_counterevidence', 409), ('sparse_basis', 200)])
def test_valid_alternatives_and_sparse_claims_preserve_scope(factory, tmp_path, case, expected):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        add_source(session, run, 'evidence_good', 'supported')
        add_source(session, run, 'evidence_other', 'not_checked' if case == 'checked_alternative' else 'supported')
        other = session.get(EvidenceRecord, 'evidence_other')
        if case in {'unrelated_bad', 'bad_alternative', 'bad_counterevidence'}:
            other.owner_id = 'foreign_owner'
        raw = {'id': 'claim_existing', 'version': 1, 'predecessor_id': None, 'proposition': 'Synthetic scoped claim.',
            'kind': 'fact', 'evidence_status': 'supported', 'reference_ids': ['evidence_good'], 'time_sensitive': True, 'verified_at': utcnow().isoformat()}
        if case in {'checked_alternative', 'bad_alternative'}:
            raw['reference_ids'] = []
            raw['support_bases'] = [{'reference_ids': ['evidence_good']}, {'reference_ids': ['evidence_other']}]
        if case == 'bad_counterevidence': raw['refuting_reference_ids'] = ['evidence_other']
        if case == 'sparse_basis':
            del raw['reference_ids']
            raw['support_bases'] = [{'reference_ids': ['evidence_good']}]
        content = StoredClaim.model_validate(raw).model_dump(mode='json', exclude_unset=True)
        session.add(ClaimVersion(id=content['id'], version=1, run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id,
            step_id=None, content=content, sha256=canonical_hash(content)))
    if case == 'unrelated_bad_json':
        with factory.begin() as session:
            session.execute(text("UPDATE evidence SET content='not-json' WHERE id='evidence_other'"))
    app = create_app(str(factory.kw['bind'].url), settings=replace(Settings(), artifact_directory=tmp_path / 'private'))
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app) as client:
        response = client.get(f'/api/runs/{run_id}/claims')
    assert response.status_code == expected
    if expected == 200:
        assert response.json()['items'][0]['evidence_status'] == 'supported'
        if case == 'sparse_basis': assert 'reference_ids' not in response.json()['items'][0]
    else:
        assert response.json()['error']['code'] == 'claim_unreadable'
