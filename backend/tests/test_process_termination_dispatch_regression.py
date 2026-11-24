"""Controlled shutdown forwards group identities, signals and grace deadlines."""
import signal
from types import SimpleNamespace

from round_robin.operations import processes


class Launcher:
    def __init__(self, pid):
        self.pid = pid
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        return 0


def test_term_and_kill_share_the_correct_groups_and_grace(monkeypatch):
    first, second = Launcher(123), Launcher(456)
    sent = []
    waits = []
    monkeypatch.setattr(processes.os, "killpg", lambda pid, signum: sent.append((pid, signum)))
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: 100.0))

    def wait_groups(groups, deadline, interval):
        waits.append((groups, deadline, interval))
        if len(waits) == 1:
            return [second]
        assert len(waits) == 2
        return []

    monkeypatch.setattr(processes, "_wait_groups", wait_groups)
    processes.terminate_owned(iter([first, second]), grace_seconds=7.0, poll_interval=0.2)
    assert sent == [(123, signal.SIGTERM), (456, signal.SIGTERM), (456, signal.SIGKILL)]
    assert waits == [([first, second], 107.0, 0.2), ([], 101.0, 0.2)]
    assert first.waits == [1.0]
    assert second.waits == [1.0]
