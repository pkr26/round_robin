"""Owned subprocess groups get bounded grace even after their launcher exits."""
import signal
import subprocess
import sys
import time

import pytest

from round_robin.operations.processes import terminate_owned


def await_file(path, process):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert process.poll() is None, "The owned test process exited before startup"
        if time.monotonic() >= deadline:
            raise AssertionError("The owned test process did not become ready")
        time.sleep(.01)


def test_exited_launcher_does_not_cut_short_its_childs_grace(tmp_path):
    script = tmp_path / "launcher.py"
    script.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
root=Path(sys.argv[1])
if sys.argv[-1]=="child":
 def stop(*args):
  time.sleep(.1)
  (root/"cleanup-done").touch()
  raise SystemExit(0)
 signal.signal(signal.SIGTERM,stop)
 (root/"child-ready").touch()
 while True:signal.pause()
subprocess.Popen([sys.executable,__file__,str(root),"child"])
while not (root/"child-ready").exists():time.sleep(.01)
(root/"ready").touch()
while True:signal.pause()
''')
    process = subprocess.Popen([sys.executable, str(script), str(tmp_path)], start_new_session=True)
    try:
        await_file(tmp_path / "ready", process)
        terminate_owned([process], grace_seconds=.4, poll_interval=.01)
        assert (tmp_path / "cleanup-done").exists()
        assert process.returncode is not None
    finally:
        terminate_owned([process], grace_seconds=0)


def test_unresponsive_owned_process_is_killed_and_reaped(tmp_path):
    marker = tmp_path / "ready"
    command = "import signal,sys;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(sys.argv[1]).touch();signal.pause()"
    process = subprocess.Popen([sys.executable, "-c", command, str(marker)], start_new_session=True)
    try:
        await_file(marker, process)
        terminate_owned([process], grace_seconds=.05, poll_interval=.01)
        assert process.returncode == -signal.SIGKILL
    finally:
        terminate_owned([process], grace_seconds=0)


def test_already_reaped_launcher_does_not_delay_cleanup():
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=5)
    terminate_owned([process], grace_seconds=.1)
    assert process.returncode == 0


@pytest.mark.parametrize("options", [{"grace_seconds": -1}, {"grace_seconds": float("nan")}, {"grace_seconds": float("inf")}, {"poll_interval": 0}, {"poll_interval": float("nan")}])
def test_invalid_shutdown_intervals_cannot_signal_processes(options):
    with pytest.raises(ValueError):
        terminate_owned([], **options)


def test_permission_denied_for_disappearing_zombie_group_is_rechecked(monkeypatch):
    from round_robin.operations import processes
    class ExitedLauncher:
        pid = 123
        def __init__(self):
            self.reaped = False
        def poll(self):
            return 0
        def wait(self, timeout):
            self.reaped = True
            return 0
    process = ExitedLauncher()
    def signal_group(group, signum):
        if not process.reaped:
            raise PermissionError("Owned group is pending reap")
        raise ProcessLookupError
    monkeypatch.setattr(processes.os, "killpg", signal_group)
    terminate_owned([process], grace_seconds=0)
    assert process.reaped


def test_one_unreapable_group_does_not_prevent_other_launcher_cleanup(monkeypatch):
    from round_robin.operations import processes
    class Launcher:
        def __init__(self, pid):
            self.pid, self.reaped = pid, False
        def poll(self):
            return None
        def wait(self, timeout):
            if self.pid == 123:
                raise subprocess.TimeoutExpired("owned launcher", timeout)
            self.reaped = True
            return 0
    first, second = Launcher(123), Launcher(456)
    monkeypatch.setattr(processes.os, "killpg", lambda *_: None)
    with pytest.raises(subprocess.TimeoutExpired):
        terminate_owned([first, second], grace_seconds=0)
    assert second.reaped


def test_persistent_permission_failure_is_reported_after_all_launchers_are_reaped(monkeypatch):
    from itertools import count
    from round_robin.operations import processes
    class Launcher:
        def __init__(self, pid):
            self.pid, self.reaped = pid, False
        def poll(self):
            return 0
        def wait(self, timeout):
            self.reaped = True
            return 0
    first, second = Launcher(123), Launcher(456)
    def signal_group(group, signum):
        if group == first.pid:
            raise PermissionError("Owned group permission remained unavailable")
        raise ProcessLookupError
    ticks = count(step=.5)
    monkeypatch.setattr(processes.os, "killpg", signal_group)
    monkeypatch.setattr(processes.time, "monotonic", lambda: next(ticks))
    with pytest.raises(PermissionError):
        terminate_owned([first, second], grace_seconds=0)
    assert first.reaped and second.reaped
