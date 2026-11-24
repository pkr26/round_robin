"""Evidence discovery includes every saved claim reference category."""
from round_robin.research.reuse import StoredClaim, _claim_references


def claim(identity, **fields):
    return StoredClaim.model_validate({
        'id': identity, 'version': 1, 'proposition': 'A recorded claim',
        'kind': 'fact', 'evidence_status': 'not_checked', 'time_sensitive': False,
        'verified_at': '2025-01-01T00:00:00Z', **fields,
    }).model_dump(mode='json', exclude_unset=True)


def test_claim_reference_union_preserves_all_categories_and_claims():
    claims = [
        claim('claim_a', reference_ids=['source_a', 'shared'],
              refuting_reference_ids=['counter_a', 'shared'],
              support_bases=[{'reference_ids': ['basis_a', 'shared']}, {'reference_ids': ['basis_b']}]),
        claim('claim_b', reference_ids=['source_b'], refuting_reference_ids=['counter_b'],
              support_bases=[{'reference_ids': ['basis_c']}]),
    ]
    assert _claim_references(claims) == {'source_a', 'shared', 'counter_a', 'basis_a', 'basis_b',
                                         'source_b', 'counter_b', 'basis_c'}


def test_sparse_claims_have_no_references():
    assert _claim_references([claim('claim_empty')]) == set()
    assert _claim_references([]) == set()
