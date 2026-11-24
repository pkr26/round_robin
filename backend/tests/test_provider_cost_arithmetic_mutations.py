from decimal import Decimal
from round_robin.providers.http import HTTPProvider


def test_cost_uses_distinct_rates_and_per_million_divisor():
    for provider in ('openai', 'anthropic', 'google'):
        config = {'pricing': {provider: {'input_usd_per_million': '2.5', 'output_usd_per_million': '7'}}}
        adapter = HTTPProvider(provider, config, api_key='synthetic')
        usage = {'promptTokenCount': 100, 'totalTokenCount': 130} if provider == 'google' else {'input_tokens': 100, 'output_tokens': 30}
        assert adapter._cost(usage) == Decimal('0.00046')
