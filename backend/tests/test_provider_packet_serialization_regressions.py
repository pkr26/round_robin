"""Provider packet serialization rejects invalid content and honors UTF-8 budgets."""

import pytest

from round_robin.providers.base import ProviderFailure, ProviderRequest
from round_robin.schemas.briefs import DraftBrief


def request(brief, *, assessments=()):
    return ProviderRequest(
        run_id='run_a', step_id='step_a', stage='proposal', role_slot='proposal',
        brief=brief, evidence=[], peer_outputs=[], allowed_reference_ids=frozenset(),
        max_output_tokens=1, timeout_seconds=1, assessments=assessments,
    )


def test_schema_valid_multilingual_brief_obeys_compact_utf8_boundary():
    # Constructed within DraftBrief's documented field bounds; admission of this
    # particular combination is not established by this direct packet test.
    def brief(last_fact_size):
        return DraftBrief(revision=1, title='Choice', question='é' * 4000,
                          background='é' * 12000, goal='é' * 4000,
                          currency='USD', user_facts=['x' * 500] * 14 + ['x' * last_fact_size]
                          ).model_dump(mode='json')

    assert request(brief(394)).packet()['brief']['goal'] == 'é' * 4000
    with pytest.raises(ProviderFailure) as caught:
        request(brief(395)).packet()
    assert caught.value.code == 'context_limit'
    assert caught.value.may_have_sent is False


@pytest.mark.parametrize('value', [float('nan'), float('inf'), object()])
def test_invalid_packet_is_a_permanent_presend_failure(value):
    # These bypass schema validation to model a corrupted saved payload.
    with pytest.raises(ProviderFailure) as caught:
        request({'value': value}).packet()
    assert caught.value.code == 'invalid_packet'
    assert caught.value.retryable is False
    assert caught.value.may_have_sent is False


def test_assessments_are_preserved_in_the_packet():
    assessments = ({'id': 'assessment_a'}, {'id': 'assessment_b'})
    assert request({}, assessments=assessments).packet()['assessments'] == list(assessments)
