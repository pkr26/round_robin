"""Permanent moderator failures use the explicitly configured fallback."""
from types import SimpleNamespace

import pytest

from round_robin.orchestration import policies
from round_robin.providers.base import ProviderFailure


@pytest.mark.parametrize('stage', ['intake', 'synthesis', 'audit'])
@pytest.mark.parametrize('code', [
    'authentication', 'credit_exhausted', 'model_unavailable',
    'capability_unverified', 'provider_configuration_invalid', 'circuit_open',
])
def test_permanent_moderator_failure_uses_stage_fallback(monkeypatch, stage, code):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 1000)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2},
                                         'fallbacks': {'intake': ['anthropic'], 'synthesis': ['anthropic'], 'audit': ['anthropic']}})
    step = SimpleNamespace(calls=1, infrastructure_claims=1, stage=stage, provider='openai')
    result = policies.plan_recovery(run, step, ProviderFailure(code), synthesizer='google')
    assert result.retry is True
    assert result.delay_seconds == 0
    assert result.fallback_provider == 'anthropic'


@pytest.mark.parametrize('stage', ['intake', 'synthesis', 'audit', 'proposal'])
def test_refusal_does_not_use_configured_fallback(monkeypatch, stage):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 1000)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2},
                                         'fallbacks': {'intake': ['anthropic'], 'synthesis': ['anthropic'], 'audit': ['anthropic']}})
    step = SimpleNamespace(calls=1, infrastructure_claims=1, stage=stage, provider='openai')
    assert policies.plan_recovery(run, step, ProviderFailure('refusal')).retry is False

