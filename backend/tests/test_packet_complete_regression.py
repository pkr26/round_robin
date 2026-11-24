"""A provider packet keeps its wire fields and the current obligation answer."""
from copy import deepcopy

from round_robin.providers.base import ProviderRequest
from round_robin.schemas.discussion import DiscussionIssue


def test_packet_has_wire_fields_and_latest_answer():
    payload = {
        'id': 'issue_a', 'concern': 'Check delivery', 'severity': 'material',
        'status': 'answered_pending_review', 'origin': 'structured', 'source_step_id': 'step_a',
        'source_provider': 'openai', 'source_stage': 'proposal',
        'events': [
            {'id': 'event_open', 'step_id': 'step_a', 'provider': 'openai', 'stage': 'proposal',
             'disposition': 'opened', 'response': 'Check delivery', 'input_hash': 'a' * 64},
            {'id': 'event_first', 'step_id': 'step_b', 'provider': 'anthropic', 'stage': 'synthesis',
             'disposition': 'answered', 'response': 'An earlier answer', 'input_hash': 'a' * 64},
            {'id': 'event_second', 'step_id': 'step_e', 'provider': 'anthropic', 'stage': 'revision',
             'disposition': 'answered', 'response': 'A second answer', 'input_hash': 'a' * 64},
            {'id': 'event_reopened', 'step_id': 'step_c', 'provider': 'openai', 'stage': 'audit',
             'disposition': 'reopened', 'response': 'Needs more review', 'input_hash': 'a' * 64},
            {'id': 'event_latest', 'step_id': 'step_d', 'provider': 'google', 'stage': 'revision',
             'disposition': 'answered', 'response': 'Current answer', 'input_hash': 'a' * 64},
        ],
    }
    issue = DiscussionIssue.model_validate(payload).model_dump(mode='json')
    before = deepcopy(issue)
    evidence = [{'id': 'source_a', 'text': 'Evidence'}]
    peer = {'provider': 'anthropic', 'output': {'summary': 'Peer result', 'claims': []}}
    request = ProviderRequest(run_id='run_a', step_id='step_d', stage='audit', role_slot='audit',
                              brief={'goal': 'Assess delivery'}, evidence=evidence, peer_outputs=[peer],
                              allowed_reference_ids=frozenset({'source_a'}), max_output_tokens=1,
                              timeout_seconds=1, open_objections=(issue,))
    packet = request.packet()
    assert packet['stage'] == 'audit'
    assert packet['role'] == 'audit'
    assert packet['brief'] == {'goal': 'Assess delivery'}
    assert packet['evidence'] == evidence
    assert packet['peer_outputs'] == [{'provider': 'anthropic', 'output': {'summary': 'Peer result'}}]
    assert packet['open_objections'] == [{**{key: value for key, value in issue.items() if key != 'events'},
                                         'latest_answer': issue['events'][-1]}]
    assert issue == before
