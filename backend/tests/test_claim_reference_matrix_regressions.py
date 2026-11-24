"""Direct checks for the saved claim's basic support requirement."""
from types import SimpleNamespace

import pytest

from round_robin.database.services import canonical_hash
from round_robin.research.reuse import ClaimRejected, StoredClaim, read_claim


def claim(kind, evidence_status, *, references=None, bases=None):
    content = {
        'id': 'claim_one', 'version': 1, 'proposition': 'A saved claim.',
        'kind': kind, 'evidence_status': evidence_status,
        'time_sensitive': False, 'verified_at': '2025-01-01T00:00:00Z',
    }
    if references is not None:
        content['reference_ids'] = references
    if bases is not None:
        content['support_bases'] = bases
    content = StoredClaim.model_validate(content).model_dump(mode='json', exclude_unset=True)
    run = SimpleNamespace(id='run_one', meeting_id='meeting_one', owner_id='owner_one')
    row = SimpleNamespace(id=content['id'], version=1, content=content,
                          run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id,
                          sha256=canonical_hash(content))
    return row, run


@pytest.mark.parametrize('kind', ['fact', 'calculation'])
def test_supported_factual_claim_needs_reference_or_basis(kind):
    row, run = claim(kind, 'supported')
    with pytest.raises(ClaimRejected):
        read_claim(row, run=run)
    row, run = claim(kind, 'supported', references=['evidence_one'])
    assert read_claim(row, run=run)['id'] == row.id
    row, run = claim(kind, 'supported', bases=[{'reference_ids': ['evidence_one']}])
    assert read_claim(row, run=run)['id'] == row.id


@pytest.mark.parametrize('kind', ['assumption', 'preference', 'recommendation'])
def test_supported_nonfactual_claim_can_omit_reference(kind):
    row, run = claim(kind, 'supported')
    assert read_claim(row, run=run)['id'] == row.id


@pytest.mark.parametrize('evidence_status', ['contradicted', 'mixed', 'not_checked', 'fictional'])
def test_unverified_fact_can_omit_reference(evidence_status):
    row, run = claim('fact', evidence_status)
    assert read_claim(row, run=run)['id'] == row.id


def test_self_reference_is_rejected_even_for_nonfactual_claim():
    row, run = claim('assumption', 'not_checked', references=['claim_one'])
    with pytest.raises(ClaimRejected):
        read_claim(row, run=run)
