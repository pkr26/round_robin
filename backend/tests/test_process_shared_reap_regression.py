"""Launchers share one bounded reap interval after graceful shutdown."""
from types import SimpleNamespace

from round_robin.operations import processes


class Launcher:
    def __init__(self, pid):
        self.pid = pid
        self.timeouts = []

    def wait(self, timeout):
        self.timeouts.append(timeout)
        return 0


def test_reap_deadline_is_shared_and_never_negative(monkeypatch):
    first, second = Launcher(123), Launcher(456)
    ticks = iter([100.0, 100.0, 100.75, 101.25])
    group_waits = []

    def wait_groups(groups, deadline, interval):
        group_waits.append((groups, deadline, interval))
        return []

    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(processes, "_wait_groups", wait_groups)
    monkeypatch.setattr(processes.os, "killpg", lambda *_: None)
    processes.terminate_owned([first, second], grace_seconds=7, poll_interval=0.2)
    assert first.timeouts == [0.25]
    assert second.timeouts == [0]
    assert group_waits == [([first, second], 107.0, 0.2), ([], 101.0, 0.2)]
