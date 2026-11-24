import pytest
from pydantic import ValidationError

from round_robin.schemas.briefs import DraftBrief


def brief(**updates):
    base = {'revision': 1, 'title': 'Synthetic comparison', 'question': 'Which?',
        'goal': 'Choose', 'currency': 'USD'}
    base.update(updates)
    return DraftBrief.model_validate(base)


def option(identity, role='candidate', components=None):
    return {'id': identity, 'label': identity, 'role': role, 'provenance': 'user_supplied',
        'cost_components': components or []}


def preference(identity, rank):
    return {'id': identity, 'description': identity, 'rank': rank, 'origin': 'user_supplied'}


def constraint(identity):
    return {'id': identity, 'description': identity, 'origin': 'user_supplied',
        'metric': 'manual', 'operator': 'manual'}


def component(identity):
    return {'id': identity, 'label': identity, 'kind': 'upfront', 'amount': '1',
        'currency': 'USD', 'frequency': 'once', 'source_ref': 'source_one'}


def test_unique_baseline_and_category_scoped_ids():
    value = brief(selected_attachment_ids=['file_a', 'file_b'],
        options=[option('id_shared'), option('id_other')],
        hard_requirements=[constraint('id_shared')], preferences=[preference('id_shared', 1)],
        public_research_terms=[{'id': 'id_shared', 'text': 'public words'}])
    assert value.options[0].id == value.hard_requirements[0].id == 'id_shared'


@pytest.mark.parametrize('updates', [
    {'selected_attachment_ids': ['file_a', 'file_a']},
    {'options': [option('id_one'), option('id_one')]},
    {'hard_requirements': [constraint('id_one'), constraint('id_one')]},
    {'preferences': [preference('id_one', 1), preference('id_one', 2)]},
    {'public_research_terms': [{'id': 'id_one', 'text': 'first'}, {'id': 'id_one', 'text': 'second'}]},
    {'preferences': [preference('id_one', 1), preference('id_two', 1)]},
    {'options': [option('id_one', components=[component('cost_one'), component('cost_one')])]},
])
def test_duplicate_scoped_identities_or_preference_ranks_rejected(updates):
    with pytest.raises(ValidationError):
        brief(**updates)


def test_four_candidates_plus_one_status_quo_accepted():
    value = brief(options=[option(f'id_{i}') for i in range(4)] + [option('id_stay', 'status_quo')])
    assert len(value.options) == 5


def test_five_candidates_rejected():
    with pytest.raises(ValidationError):
        brief(options=[option(f'id_{i}') for i in range(5)])


def test_two_status_quo_options_rejected():
    with pytest.raises(ValidationError):
        brief(options=[option('id_one', 'status_quo'), option('id_two', 'status_quo')])
