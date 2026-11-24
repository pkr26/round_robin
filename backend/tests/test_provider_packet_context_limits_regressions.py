"""Provider packets respect independent context limits and pre-send failures."""
import json

import pytest

from round_robin.providers.base import MAX_PACKET_BYTES, ProviderFailure, ProviderRequest


def request(*, brief=None, evidence=None):
    return ProviderRequest(
        run_id='run_a', step_id='step_a', stage='critique', role_slot='critic',
        brief=brief if brief is not None else {}, evidence=evidence if evidence is not None else [],
        peer_outputs=[], allowed_reference_ids=frozenset(), max_output_tokens=1, timeout_seconds=1,
    )


def assert_limit(call):
    with pytest.raises(ProviderFailure) as captured:
        call()
    assert captured.value.code == 'context_limit'
    assert captured.value.retryable is False
    assert captured.value.may_have_sent is False


def test_independent_evidence_count_and_text_bounds():
    # Evidence dictionaries are direct policy inputs, not a live provider call.
    sources = [{'id': f'source_{i}', 'text': 'x' * 2000} for i in range(25)]
    assert len(request(evidence=sources[:24]).packet()['evidence']) == 24
    assert_limit(lambda: request(evidence=sources).packet())
    assert_limit(lambda: request(evidence=[{'id': 'source_a', 'text': 'x' * 2001}]).packet())


def test_total_packet_byte_boundary():
    # An oversize brief is deliberately constructed to isolate the generic
    # packet guard; admission of this complete request is not established.
    empty = request(brief={'payload': ''}).packet()
    overhead = len(json.dumps(empty, allow_nan=False, ensure_ascii=False, separators=(',', ':')).encode())
    payload = 'x' * (MAX_PACKET_BYTES - overhead)
    packet = request(brief={'payload': payload}).packet()
    assert len(json.dumps(packet, allow_nan=False, ensure_ascii=False, separators=(',', ':')).encode()) == MAX_PACKET_BYTES
    assert_limit(lambda: request(brief={'payload': payload + 'x'}).packet())


@pytest.mark.parametrize('value', [float('nan'), object()])
def test_invalid_packet_failure_is_permanent_and_presend(value):
    with pytest.raises(ProviderFailure) as captured:
        request(brief={'payload': value}).packet()
    assert captured.value.code == 'invalid_packet'
    assert captured.value.retryable is False
    assert captured.value.may_have_sent is False
