"""Provider packets retain only the latest answer to each open issue."""
from copy import deepcopy

from round_robin.providers.base import ProviderRequest
from round_robin.schemas.discussion import DiscussionIssue


def test_packet_obligations_keep_latest_answer_without_mutating_saved_history():
    payload = {
        'id': 'issue_a', 'concern': 'Check the delivery date', 'severity': 'material',
        'status': 'answered_pending_review', 'origin': 'structured', 'source_step_id': 'step_a',
        'source_provider': 'openai', 'source_stage': 'proposal',
        'events': [
            {'id': 'event_open', 'step_id': 'step_a', 'provider': 'openai', 'stage': 'proposal',
             'disposition': 'opened', 'response': 'Check the delivery date', 'input_hash': 'a' * 64},
            {'id': 'event_first', 'step_id': 'step_b', 'provider': 'anthropic', 'stage': 'synthesis',
             'disposition': 'answered', 'response': 'An earlier answer', 'input_hash': 'a' * 64},
            {'id': 'event_reopened', 'step_id': 'step_d', 'provider': 'openai', 'stage': 'audit',
             'disposition': 'reopened', 'response': 'More review is needed', 'input_hash': 'a' * 64},
            {'id': 'event_latest', 'step_id': 'step_c', 'provider': 'google', 'stage': 'revision',
             'disposition': 'answered', 'response': 'The current answer', 'input_hash': 'a' * 64},
        ],
    }
    issue = DiscussionIssue.model_validate(payload).model_dump(mode='json')
    before = deepcopy(issue)
    request = ProviderRequest(run_id='run_a', step_id='step_a', stage='audit', role_slot='audit',
                              brief={}, evidence=[], peer_outputs=[], allowed_reference_ids=frozenset(),
                              max_output_tokens=1, timeout_seconds=1, open_objections=(issue,))
    returned = request.packet()['open_objections']
    assert returned == [{**{key: value for key, value in issue.items() if key != 'events'},
                         'latest_answer': issue['events'][3]}]
    assert issue == before


def test_packet_handles_schema_valid_issue_with_omitted_event_field():
    # Sparse serialization is deliberate; the current discussion reader emits defaults.
    issue = DiscussionIssue.model_validate({
        'id': 'issue_b', 'concern': 'Check the price', 'severity': 'material', 'origin': 'structured',
        'source_step_id': 'step_a', 'source_provider': 'openai', 'source_stage': 'proposal',
    }).model_dump(mode='json', exclude_unset=True)
    assert 'events' not in issue
    request = ProviderRequest(run_id='run_a', step_id='step_a', stage='audit', role_slot='audit',
                              brief={}, evidence=[], peer_outputs=[], allowed_reference_ids=frozenset(),
                              max_output_tokens=1, timeout_seconds=1, open_objections=(issue,))
    assert request.packet()['open_objections'] == [issue]
