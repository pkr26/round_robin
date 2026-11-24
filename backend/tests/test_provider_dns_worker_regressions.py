"""Provider DNS setup and deadline use bounded, isolated worker behavior."""
from types import SimpleNamespace

import httpcore
import pytest
from round_robin.providers import transport


class Capacity:
    def __init__(self):
        self.timeouts = []
        self.releases = 0

    def acquire(self, timeout):
        self.timeouts.append(timeout)
        return True

    def release(self):
        self.releases += 1


def test_dns_worker_start_failure_returns_capacity_and_sanitizes_error(monkeypatch):
    capacity = Capacity()
    monkeypatch.setattr(transport, '_DNS_CAPACITY', capacity)
    monkeypatch.setattr(transport, '_remaining', lambda deadline: 2.5)

    class BrokenThread:
        def __init__(self, *, target, daemon, name):
            assert daemon is True

        def start(self):
            raise RuntimeError('private thread failure')

    monkeypatch.setattr(transport.threading, 'Thread', BrokenThread)
    with pytest.raises(httpcore.ConnectError) as captured:
        transport._addresses('provider.invalid', 443, 10)
    assert capacity.timeouts == [2.5]
    assert capacity.releases == 1
    assert 'worker' in str(captured.value).lower()
    assert 'private' not in str(captured.value)


def test_dns_deadline_retains_capacity_until_delayed_worker_finishes(monkeypatch):
    capacity = Capacity()
    waits, workers, remaining_calls = [], [], []

    class PendingEvent:
        def wait(self, timeout):
            waits.append(timeout)
            return False

        def set(self):
            self.finished = True

    class PendingThread:
        def __init__(self, *, target, daemon, name):
            assert daemon is True
            self.target = target
            workers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(transport, '_DNS_CAPACITY', capacity)
    def remaining(deadline):
        remaining_calls.append(deadline)
        return 2.5

    monkeypatch.setattr(transport, '_remaining', remaining)
    monkeypatch.setattr(transport, 'threading', SimpleNamespace(Event=PendingEvent, Thread=PendingThread))
    monkeypatch.setattr(transport.socket, 'getaddrinfo', lambda *args, **kwargs: [
        (0, 0, 0, '', ('192.0.2.1', 443)),
    ])
    with pytest.raises(httpcore.ConnectTimeout) as captured:
        transport._addresses('provider.invalid', 443, 10)
    assert 'deadline' in str(captured.value).lower()
    assert capacity.timeouts == waits == [2.5]
    assert remaining_calls == [10, 10]
    assert capacity.releases == 0
    assert len(workers) == 1
    workers[0].target()
    assert capacity.releases == 1
