import hashlib

from round_robin.research.fetcher import RetrievedSource, exact_passages
from round_robin.schemas.views import StoredEvidence


def test_retrieved_excerpt_preserves_selection_method_and_discloses_unverified_scope():
    text = 'WARRANTY lasts five years. ' + 'Generic details. ' * 100
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    source = RetrievedSource('https://example.invalid/source', text, content_hash,
                             '2026-11-17T12:00:00+00:00', 'synthetic_origin')
    passage, = exact_passages(source, maximum=1, size=100, terms=['warranty'])
    stored = StoredEvidence.model_validate({
        'id': 'evidence_1', 'run_id': 'run_1', 'owner_id': 'owner_1',
        'source_title': 'Synthetic source', 'url': source.url,
        'origin': 'retrieved', 'verified_at': source.fetched_at,
        'origin_group': source.origin_group, 'time_sensitive': True,
        **passage,
    }).model_dump(mode='json')
    assert stored['selection_method'] == 'bounded_requirement_exception_coverage_v1'
    assert stored['verification_status'] == 'not_checked'
    scope = stored['scope'].casefold()
    assert 'relevance is heuristic' in scope
    assert 'semantic support remains unchecked' in scope
    assert 'omitted text is not verified' in scope
    assert stored['uninspected_characters'] > 0
