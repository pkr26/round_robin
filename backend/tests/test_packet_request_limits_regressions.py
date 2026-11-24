"""Packet limits preserve per-stage tokens and explicit pre-send failure."""
import pytest
from round_robin.providers.base import OUTPUT_TOKENS, ProviderFailure, ProviderRequest


def make_request(stage, tokens=1, timeout=1):
    return ProviderRequest(run_id='run_a', step_id='step_a', stage=stage, role_slot=stage,
        brief={}, evidence=[], peer_outputs=[], allowed_reference_ids=frozenset(),
        max_output_tokens=tokens, timeout_seconds=timeout)


def assert_invalid(request):
    with pytest.raises(ProviderFailure) as caught:
        request.packet()
    assert caught.value.code == 'invalid_request_limits'
    assert caught.value.retryable is False
    assert caught.value.may_have_sent is False


@pytest.mark.parametrize('stage', list(OUTPUT_TOKENS))
def test_each_stage_accepts_its_token_bounds(stage):
    assert make_request(stage, 1).packet()['stage'] == stage
    assert make_request(stage, OUTPUT_TOKENS[stage]).packet()['stage'] == stage
    assert_invalid(make_request(stage, 0))
    assert_invalid(make_request(stage, OUTPUT_TOKENS[stage] + 1))


def test_timeout_boundary_and_fractional_time():
    for value in (.1, 1, 90):
        assert make_request('proposal', timeout=value).packet()['stage'] == 'proposal'
    for value in (0, -.1, 90.01, 91):
        assert_invalid(make_request('proposal', timeout=value))


def test_unknown_stage_uses_4096_token_default():
    # Direct helper fallback: ordinary worker stages use explicit OUTPUT_TOKENS keys.
    assert make_request('unlisted', tokens=4096).packet()['stage'] == 'unlisted'
    assert_invalid(make_request('unlisted', tokens=4097))
