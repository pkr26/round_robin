"""Objection openings retain stable source identity and sparse compatibility."""
from types import SimpleNamespace

from round_robin.research.discussion import _openings
from round_robin.schemas.contributions import DiscussionObjection


def test_structured_objection_uses_scoped_stable_identity():
    objection = DiscussionObjection(alias='new_objection_1', concern='Check evidence', severity='material').model_dump(mode='json')
    run = SimpleNamespace(id='run_a')
    step = SimpleNamespace(id='step_b')
    result = list(_openings(run, step, {'objections': [objection]}))
    assert len(result) == 1
    assert result[0] == {**objection, 'id': 'objection_27c79223bde5e845f3897daa00eca3595359a8d5bc383a99', 'origin': 'structured'}


def test_sparse_legacy_openings_and_existing_identity():
    run = SimpleNamespace(id='run_a')
    step = SimpleNamespace(id='step_b')
    assert list(_openings(run, step, {})) == []
    saved = {'id': 'old_objection', 'alias': 'new_objection_1', 'concern': 'Legacy concern', 'severity': 'material'}
    assert list(_openings(run, step, {'objections': [saved]})) == [{**saved, 'origin': 'structured'}]


def test_provider_defects_and_dissent_are_distinct_and_deduplicated():
    from round_robin.providers.base import ProviderOutput

    output = ProviderOutput.model_validate({
        'summary': 'Summary', 'recommendation_option_id': None,
        'reference_ids': [], 'questions': [], 'assumptions': [], 'constraints': [],
        'audit_passed': False, 'critical_defects': ['Critical concern', 'Critical concern'],
        'dissent': ['Minor concern', 'Minor concern'],
    }).model_dump(mode='json')
    result = list(_openings(SimpleNamespace(id='run_a'), SimpleNamespace(id='step_b'), output))
    assert result == [
        {'id': 'objection_47001b1ca128113fb1016f63279f76d8f992df9135ac141a',
         'concern': 'Critical concern', 'severity': 'critical', 'origin': 'critical_defect'},
        {'id': 'objection_71ec8521aacc322c4ec91c334acc9e2edd2a6bb0973c4bf5',
         'concern': 'Minor concern', 'severity': 'minor', 'origin': 'dissent'},
    ]
