"""Retry admission must reserve time for its request and mandatory finish."""
import socket
from types import SimpleNamespace

import pytest
from round_robin.orchestration import policies
from round_robin.orchestration.protocol import recovery_fits
from round_robin.providers.base import ProviderFailure


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network calls forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.mark.parametrize("stage,required", [("proposal", 380), ("synthesis", 260), ("audit", 140)])
def test_retry_must_fit_wait_request_capacity_margin_and_stage_tail(stage, required):
    values = {"stage": stage, "wait_seconds": 5, "reserved_finish_seconds": 240, "capacity_delay_seconds": 20}
    assert not recovery_fits(remaining_seconds=required, **values)
    assert recovery_fits(remaining_seconds=required + 0.5, **values)


def test_zero_wait_fallback_can_use_available_time_without_adding_phantom_delay():
    assert recovery_fits(stage="synthesis", remaining_seconds=210.5, wait_seconds=0)


@pytest.mark.parametrize("remaining,expected", [(380, False), (380.5, True)])
def test_real_recovery_policy_applies_finish_and_capacity_reserves(monkeypatch, remaining, expected):
    run = SimpleNamespace(configuration={"limits": {}, "finish_policy": {"reserved_active_seconds": 240}})
    step = SimpleNamespace(calls=0, infrastructure_claims=0, stage="proposal")
    failure = ProviderFailure("timeout", retryable=True, retry_after=5)
    monkeypatch.setattr(policies, "remaining_active", lambda _: remaining)
    assert policies.plan_recovery(run, step, failure, capacity_delay_seconds=20).retry is expected
