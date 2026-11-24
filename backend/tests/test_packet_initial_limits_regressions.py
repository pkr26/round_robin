"""Initial stage size limits and pre-send request failures."""
import json

import pytest

from round_robin.providers.base import MAX_INITIAL_PACKET_BYTES, MAX_PACKET_BYTES, ProviderFailure, ProviderRequest


def request(stage, size=0, **limits):
    return ProviderRequest(run_id='run_a', step_id='step_a', stage=stage, role_slot=stage,
                           brief={'payload': 'x' * size}, evidence=[], peer_outputs=[],
                           allowed_reference_ids=frozenset(),
                           max_output_tokens=limits.get('max_output_tokens', 1),
                           timeout_seconds=limits.get('timeout_seconds', 1))


def raw_size(packet):
    return len(json.dumps(packet, allow_nan=False, ensure_ascii=False, separators=(',', ':')).encode())


def assert_context_error(req):
    with pytest.raises(ProviderFailure) as captured:
        req.packet()
    assert captured.value.code == 'context_limit'
    assert captured.value.retryable is False
    assert captured.value.may_have_sent is False


@pytest.mark.parametrize('stage', ['intake', 'proposal'])
def test_initial_packet_exact_byte_boundary(stage):
    # Direct oversized briefs isolate packet guards; complete admission is unproven.
    overhead = raw_size(request(stage).packet())
    at_limit = request(stage, MAX_INITIAL_PACKET_BYTES - overhead)
    assert raw_size(at_limit.packet()) == MAX_INITIAL_PACKET_BYTES
    assert_context_error(request(stage, MAX_INITIAL_PACKET_BYTES - overhead + 1))


def test_later_stage_accepts_more_than_initial_limit():
    stage = 'critique'
    overhead = raw_size(request(stage).packet())
    assert raw_size(request(stage, MAX_INITIAL_PACKET_BYTES - overhead + 1).packet()) == MAX_INITIAL_PACKET_BYTES + 1


def test_total_packet_failure_is_permanent_and_presend():
    overhead = raw_size(request('critique').packet())
    assert_context_error(request('critique', MAX_PACKET_BYTES - overhead + 1))


@pytest.mark.parametrize('limits', [{'max_output_tokens': 0}, {'timeout_seconds': 0}])
def test_independently_invalid_limits_are_rejected(limits):
    with pytest.raises(ProviderFailure) as captured:
        request('proposal', **limits).packet()
    assert captured.value.code == 'invalid_request_limits'
    assert captured.value.retryable is False
    assert captured.value.may_have_sent is False
