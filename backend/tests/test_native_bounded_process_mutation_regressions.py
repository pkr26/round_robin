"""Host subprocess limits using only short-lived, non-forking synthetic children."""

import os
import signal
import subprocess
import sys
import time

import pytest
from round_robin.visual.renderers import interface

SENTINEL = "rr-native-synthetic-child-v1"


@pytest.fixture
def owned_children(monkeypatch, tmp_path):
    original = subprocess.Popen
    log = os.environ.get("RR_NATIVE_CHILD_LOG", str(tmp_path / "children.log"))
    children = []
    def launch(args, *positional, **keywords):
        assert args[0] == sys.executable and args[1] == "-c" and args[2].startswith("# " + SENTINEL)
        child = original(args, *positional, start_new_session=True, **keywords)
        children.append(child)
        with open(log, "a", encoding="ascii") as handle:
            handle.write(f"{child.pid}\n")
        return child
    monkeypatch.setattr(interface.subprocess, "Popen", launch)
    yield children
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


def command(body):
    return [sys.executable, "-c", "# " + SENTINEL + "\nimport signal; signal.setitimer(signal.ITIMER_REAL, 1.5)\n" + body]


def test_synthetic_child_terminates_even_when_stdin_is_never_closed(owned_children):
    child = subprocess.Popen(command("import sys; sys.stdin.buffer.read()"), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert child.wait(timeout=3) == -signal.SIGALRM
    child.stdin.close()
    child.stdout.close()
    child.stderr.close()


def test_output_limits_are_inclusive_and_per_stream(owned_children):
    code = "import os,sys; data=sys.stdin.buffer.read(); os.write(1,data); os.write(2,b'xyz')"
    result = interface._bounded_process(command(code), b"12345", timeout=2, stdout_limit=5, stderr_limit=3)
    assert result == (0, b"12345", b"xyz")


@pytest.mark.parametrize(("stdout", "stderr"), [(6, 0), (0, 4)])
def test_output_over_either_limit_fails_closed(owned_children, stdout, stderr):
    code = f"import os; os.write(1,b'a'*{stdout}); os.write(2,b'b'*{stderr})"
    with pytest.raises(interface.NativeRendererError):
        interface._bounded_process(command(code), b"", timeout=2, stdout_limit=5, stderr_limit=3)


def test_one_closed_pipe_does_not_hide_the_other_stream(owned_children):
    code = "import os,time; os.close(1); time.sleep(0.05); os.write(2,b'late')"
    assert interface._bounded_process(command(code), b"", timeout=2, stdout_limit=5, stderr_limit=4) == (0, b"", b"late")


def test_host_deadline_terminates_its_synthetic_child(owned_children):
    started = time.monotonic()
    with pytest.raises(interface.NativeRendererTimeout):
        interface._bounded_process(command("import time; time.sleep(1.2)"), b"", timeout=0.2, stdout_limit=5, stderr_limit=3)
    assert time.monotonic() - started < 1
    assert len(owned_children) == 1 and owned_children[0].poll() is not None


def test_short_valid_deadline_still_allows_quick_child(owned_children):
    code = "import os; os.write(1,b'ok')"
    assert interface._bounded_process(command(code), b"", timeout=0.9, stdout_limit=2, stderr_limit=3) == (0, b"ok", b"")


def test_deadline_still_applies_after_both_output_pipes_close(owned_children):
    code = "import os,time; os.close(1); os.close(2); time.sleep(1.2)"
    with pytest.raises(interface.NativeRendererTimeout):
        interface._bounded_process(command(code), b"", timeout=0.2, stdout_limit=5, stderr_limit=3)
    assert len(owned_children) == 1 and owned_children[0].poll() is not None
