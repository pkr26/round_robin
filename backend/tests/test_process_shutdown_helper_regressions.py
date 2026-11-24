"""Process group discovery and grace polling without operating-system signals."""

from types import SimpleNamespace

import pytest

from round_robin.operations import processes


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [(None, True), (ProcessLookupError, False), (PermissionError, True)],
)
def test_group_exists_uses_only_signal_zero(monkeypatch, outcome, expected):
    calls = []

    def signal_group(group, signum):
        calls.append((group, signum))
        if outcome:
            raise outcome("synthetic group outcome")

    monkeypatch.setattr(processes.os, "killpg", signal_group)
    assert processes._group_exists(2718) is expected
    assert calls == [(2718, 0)]


class FakeLauncher:
    def __init__(self, pid):
        self.pid = pid
        self.poll_count = 0

    def poll(self):
        self.poll_count += 1
        return None


def test_wait_groups_uses_poll_interval_and_remaining_deadline(monkeypatch):
    clock = [0.0]
    sleeps = []
    checks = []
    process = FakeLauncher(2718)

    def sleep(seconds):
        assert seconds > 0 and len(sleeps) < 5
        sleeps.append(seconds)
        clock[0] += seconds

    def group_exists(group_id):
        checks.append(group_id)
        return True

    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(processes, "_group_exists", group_exists)
    assert processes._wait_groups([process], deadline=9, poll_interval=4) == [process]
    assert sleeps == [4, 4, 1]
    assert checks == [2718] * 4
    assert process.poll_count == 4


def test_wait_groups_stops_when_group_disappears(monkeypatch):
    clock = [0.0]
    sleeps = []
    process = FakeLauncher(2718)

    def sleep(seconds):
        assert seconds > 0 and len(sleeps) < 2
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(processes, "_group_exists", lambda group_id: clock[0] == 0)
    assert processes._wait_groups([process], deadline=10, poll_interval=4) == []
    assert sleeps == [4]
    assert process.poll_count == 2
