from types import SimpleNamespace

from round_robin.research.queries import render_query
from round_robin.research.service import scope_for_run


def test_scope_contains_only_frozen_approved_terms_and_public_region():
    run = SimpleNamespace(input_snapshot={
        'public_research_terms': [{'id': 'term_a', 'text': 'label systems'}, {'id': 'term_b', 'text': 'replacement tape'}],
        'location': 'PRIVATE_LOCATION', 'background': 'PRIVATE_BACKGROUND'},
        configuration={'research': {'approved_public_region': 'Public Region'}})
    scope = scope_for_run(run)
    assert scope.terms == (('term_a', 'label systems'), ('term_b', 'replacement tape'))
    assert scope.region == 'Public Region'
    query = render_query(scope, term_ids=['term_b', 'term_a'], template_id='comparison_v1')
    assert query == 'replacement tape label systems comparison Public Region'
    assert 'PRIVATE' not in query


def test_missing_public_scope_fields_use_empty_defaults():
    run = SimpleNamespace(input_snapshot={'location': 'PRIVATE_LOCATION'}, configuration={})
    scope = scope_for_run(run)
    assert scope.terms == ()
    assert scope.region == ''
