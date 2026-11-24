"""Positive fractional prices and explicit pre-send pricing errors."""
from decimal import Decimal

import httpx
import pytest

from backend.tests.test_providers import configuration, packet, response_for
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider, _decimal


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'google'])
def test_fractional_prices_are_accounted_for(provider):
    config = configuration(provider)
    config['pricing'][provider]['input_usd_per_million'] = '0.5'
    config['pricing'][provider]['output_usd_per_million'] = '0.25'
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response_for(provider))

    adapter = HTTPProvider(provider, config, transport=httpx.MockTransport(respond), api_key='synthetic')
    assert adapter.conservative_bound(packet()) > 0
    assert adapter.generate(packet()).cost_usd == Decimal('0.0000625')
    assert len(sent) == 1


def test_invalid_price_reports_exact_pre_send_code():
    with pytest.raises(ProviderFailure) as captured:
        _decimal('0')
    assert captured.value.code == 'pricing_unavailable'
    assert captured.value.may_have_sent is False
