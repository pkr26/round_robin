"""Shutdown validates duration bounds and preserves its default deadlines."""
from types import SimpleNamespace

import pytest

from round_robin.operations import processes


@pytest.fixture
def controlled_shutdown(monkeypatch):
    calls = []
    monkeypatch.setattr(processes, 'time', SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(processes, '_wait_groups', lambda groups, deadline, interval: calls.append((groups, deadline, interval)) or [])
    monkeypatch.setattr(processes.os, 'killpg', lambda *_: pytest.fail('unexpected signal'))
    return calls


def test_default_deadline_and_poll_interval(controlled_shutdown):
    processes.terminate_owned([])
    assert controlled_shutdown == [([], 110.0, 0.05), ([], 101.0, 0.05)]


def test_zero_grace_and_short_poll_are_valid(controlled_shutdown):
    processes.terminate_owned([], grace_seconds=0, poll_interval=0.05)
    assert controlled_shutdown[0] == ([], 100.0, 0.05)


@pytest.mark.parametrize('grace', [-1, float('inf'), float('-inf'), float('nan')])
def test_invalid_grace_is_rejected(controlled_shutdown, grace):
    with pytest.raises(ValueError):
        processes.terminate_owned([], grace_seconds=grace)
    assert not controlled_shutdown


@pytest.mark.parametrize('interval', [0, -1, float('inf'), float('-inf'), float('nan')])
def test_invalid_poll_interval_is_rejected(controlled_shutdown, interval):
    with pytest.raises(ValueError):
        processes.terminate_owned([], poll_interval=interval)
    assert not controlled_shutdown
