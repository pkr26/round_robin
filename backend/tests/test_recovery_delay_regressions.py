"""Recovery retains valid retry delays and substitutes unsafe values."""
from types import SimpleNamespace

import pytest

from round_robin.orchestration import policies
from round_robin.providers.base import ProviderFailure


@pytest.mark.parametrize(('provided', 'expected'), [
    (None, 2.5), (0, 0), (17, 17), (-1, 2.5), (float('inf'), 2.5), (float('nan'), 2.5),
])
def test_retry_delay_uses_valid_value_or_safe_default(monkeypatch, provided, expected):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 1000)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2}})
    step = SimpleNamespace(calls=0, infrastructure_claims=0, stage='proposal')
    result = policies.plan_recovery(run, step, ProviderFailure('rate_limit', retryable=True, retry_after=provided))
    assert result.retry is True
    assert result.delay_seconds == expected
