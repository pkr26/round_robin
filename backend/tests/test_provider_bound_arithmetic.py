import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider


def provider(context=100000):
    config = {'mode': 'live', 'funded': True, 'models': {'openai': 'synthetic'},
        'capabilities': {'openai': {'structured_text': True, 'context_window_tokens': context}},
        'pricing': {'openai': {'version': 'synthetic', 'reasoning_exposure_bounded': True,
            'input_usd_per_million': '2.5', 'output_usd_per_million': '7',
            'billable_output_bound_tokens': 1000}}}
    instance = HTTPProvider('openai', config, api_key='synthetic')
    instance._payload = lambda request: ('unused', {}, {'message': 'é'})
    return instance


def test_exact_bound_uses_utf8_bytes_margin_distinct_prices_and_million():
    # JSON UTF-8 is 17 bytes; input bound 4,113, output bound 1,000.
    assert len(json.dumps({'message': 'é'}, ensure_ascii=False).encode('utf-8')) == 17
    assert provider().conservative_bound(SimpleNamespace(max_output_tokens=500)) == Decimal('0.0172825')


def test_context_limit_includes_both_bounds_and_accepts_equality():
    request = SimpleNamespace(max_output_tokens=500)
    assert provider(5113).conservative_bound(request) == Decimal('0.0172825')
    with pytest.raises(ProviderFailure) as error:
        provider(5112).conservative_bound(request)
    assert error.value.code == 'context_limit'
