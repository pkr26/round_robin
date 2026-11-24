"""Recovery uses logical counts and the frozen per-step call limit."""
from types import SimpleNamespace

import pytest

from round_robin.orchestration import policies
from round_robin.providers.base import ProviderFailure


@pytest.fixture
def failure():
    return ProviderFailure('timeout', retryable=True, retry_after=0)


def test_configured_call_limit_blocks_retry_at_one(monkeypatch, failure):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 1000)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 1}})
    step = SimpleNamespace(calls=1, infrastructure_claims=1, stage='proposal')
    assert not policies.plan_recovery(run, step, failure).retry
    run.configuration = {'limits': {'max_calls_per_step': 2}}
    assert policies.plan_recovery(run, step, failure).retry


@pytest.mark.parametrize(('calls', 'claims', 'logical_calls', 'logical_claims', 'expected'), [
    (1, 0, 0, 0, True),
    (0, 0, 1, 0, False),
    (0, 3, 0, 0, True),
    (0, 0, 0, 3, False),
])
def test_explicit_logical_counts_override_step_counts(monkeypatch, failure, calls, claims, logical_calls, logical_claims, expected):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 1000)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 1}})
    step = SimpleNamespace(calls=calls, infrastructure_claims=claims, stage='proposal')
    assert policies.plan_recovery(run, step, failure, logical_calls=logical_calls, logical_claims=logical_claims).retry is expected


def test_omitted_capacity_delay_defaults_to_zero(monkeypatch, failure):
    monkeypatch.setattr(policies, 'remaining_active', lambda run: 305.5)
    run = SimpleNamespace(configuration={'limits': {'max_calls_per_step': 2}})
    step = SimpleNamespace(calls=0, infrastructure_claims=0, stage='proposal')
    assert policies.plan_recovery(run, step, failure).retry
    assert not policies.plan_recovery(run, step, failure, capacity_delay_seconds=1).retry
