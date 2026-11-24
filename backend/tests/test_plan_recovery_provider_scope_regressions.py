"""Fallback selection respects provider scope and prior ownership."""
from types import SimpleNamespace

import pytest

from round_robin.orchestration import policies
from round_robin.providers.base import ProviderFailure


@pytest.mark.parametrize(('stage', 'current', 'synthesizer', 'order', 'expected'), [
    ('intake', 'anthropic', 'google', ['openai'], 'openai'),
    ('synthesis', 'google', 'openai', ['anthropic'], 'anthropic'),
    ('intake', 'openai', 'google', ['google'], 'google'),
    ('intake', 'openai', 'google', ['openai', 'anthropic'], 'anthropic'),
    ('audit', 'openai', 'google', ['google', 'anthropic'], 'anthropic'),
    ('audit', 'openai', 'google', ['google'], None),
    ('intake', 'openai', 'google', ['openai'], None),
])
def test_permanent_failure_selects_only_eligible_fallback(monkeypatch, stage, current, synthesizer, order, expected):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 1000)
    fallbacks = {'intake': ['openai'], 'synthesis': ['openai'], 'audit': ['openai']}
    fallbacks[stage] = order
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2}, 'fallbacks': fallbacks})
    step = SimpleNamespace(calls=1, infrastructure_claims=1, stage=stage, provider=current)
    result = policies.plan_recovery(run, step, ProviderFailure('authentication'), synthesizer=synthesizer)
    assert result.retry is (expected is not None)
    assert result.fallback_provider == expected


@pytest.mark.parametrize('fallbacks', [{}, {'audit': ['google']}])
def test_missing_stage_fallback_refuses_retry(monkeypatch, fallbacks):
    # Synthetic saved state: current admission requires all stage mappings.
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 1000)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2}, 'fallbacks': fallbacks})
    step = SimpleNamespace(calls=1, infrastructure_claims=1, stage='intake', provider='openai')
    assert not policies.plan_recovery(run, step, ProviderFailure('authentication')).retry
