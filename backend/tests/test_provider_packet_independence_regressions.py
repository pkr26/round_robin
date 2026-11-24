"""Provider packets preserve proposal independence and peer claim content."""

import pytest

from round_robin.providers.base import ProviderFailure, ProviderRequest


def request(stage='proposal', *, peers=(), claims=()):
    return ProviderRequest(
        run_id='run_test', step_id='step_test', stage=stage, role_slot=stage,
        brief={}, evidence=[], peer_outputs=list(peers), allowed_reference_ids=frozenset(),
        max_output_tokens=1, timeout_seconds=1, claims=claims,
    )


def test_proposal_peer_independence_is_a_permanent_presend_failure():
    peer={'provider': 'peer', 'output': {'summary': 'A peer result', 'claims': []}}
    assert request().packet()['peer_outputs'] == []
    with pytest.raises(ProviderFailure) as caught:
        request(peers=[peer]).packet()
    assert caught.value.code == 'independence_violation'
    assert caught.value.retryable is False
    assert caught.value.may_have_sent is False
    assert request('critique', peers=[peer]).packet()['peer_outputs'][0]['provider'] == 'peer'


def test_known_peer_claims_are_omitted_and_unknown_claims_retained():
    peer={'provider': 'peer', 'output': {'summary': 'A peer result', 'claims': [{'id': 'claim_a'}]}}
    original={'provider': 'peer', 'output': {'summary': 'A peer result', 'claims': [{'id': 'claim_a'}]}}
    known=request('critique', peers=[peer], claims=({'id': 'claim_a'},)).packet()
    assert known['peer_outputs'] == [{'provider': 'peer', 'output': {'summary': 'A peer result'}}]
    assert peer == original
    unknown=request('critique', peers=[peer], claims=({'id': 'claim_b'},)).packet()
    assert unknown['peer_outputs'] == [original]
