"""Retries reserve the stage-specific mandatory tail and configured capacity."""
from types import SimpleNamespace

import pytest

from round_robin.orchestration import policies
from round_robin.providers.base import ProviderFailure


@pytest.mark.parametrize(('stage', 'remaining'), [('audit', 116), ('synthesis', 211), ('proposal', 306)])
def test_recovery_uses_correct_stage_tail(monkeypatch, stage, remaining):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: remaining)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2}})
    step = SimpleNamespace(calls=0, infrastructure_claims=0, stage=stage)
    result = policies.plan_recovery(run, step, ProviderFailure('timeout', retryable=True, retry_after=0))
    assert result.retry is True


def test_recovery_reserves_configured_finish_time(monkeypatch):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 250)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2}, 'finish_policy': {'reserved_active_seconds': 300}})
    step = SimpleNamespace(calls=0, infrastructure_claims=0, stage='synthesis')
    failure = ProviderFailure('timeout', retryable=True, retry_after=0)
    assert not policies.plan_recovery(run, step, failure).retry
    run.configuration['finish_policy']['reserved_active_seconds'] = 190
    assert policies.plan_recovery(run, step, failure).retry


def test_recovery_reserves_capacity_delay(monkeypatch):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 116)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2}})
    step = SimpleNamespace(calls=0, infrastructure_claims=0, stage='audit')
    failure = ProviderFailure('timeout', retryable=True, retry_after=0)
    assert policies.plan_recovery(run, step, failure, capacity_delay_seconds=0).retry
    assert not policies.plan_recovery(run, step, failure, capacity_delay_seconds=1).retry
