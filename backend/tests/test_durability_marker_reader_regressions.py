import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from round_robin.operations import durability


def _child_environment():
    loaded_file = Path(durability.__file__).resolve()
    return {**os.environ, "PYTHONPATH": str(loaded_file.parents[2]), "ROUND_ROBIN_TEST_DURABILITY_FILE": str(loaded_file)}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
@pytest.mark.parametrize("swap", [False, True])
def test_fifo_marker_keeps_gate_closed_without_blocking(tmp_path, swap):
    control = tmp_path / "synthetic-control"
    control.mkdir()
    marker = control / "failure_synthetic.json"
    if swap:
        marker.write_text('{"code":"artifact_storage_failed","timestamp":"2026-01-01T00:00:00+00:00"}')
    else:
        os.mkfifo(marker)
    script = """
import os, signal, sys
from pathlib import Path
signal.alarm(3)
from round_robin.operations import durability
assert Path(durability.__file__).resolve() == Path(os.environ['ROUND_ROBIN_TEST_DURABILITY_FILE']).resolve()
marker_pending = durability.marker_pending
if sys.argv[3] == 'swap':
    original_open = os.open
    def swapping_open(path, flags, *args):
        if str(path).endswith('failure_synthetic.json'):
            os.unlink(path)
            os.mkfifo(path)
        return original_open(path, flags, *args)
    os.open = swapping_open
print('closed' if marker_pending(Path(sys.argv[1]), control_directory=Path(sys.argv[2])) else 'open', flush=True)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "artifacts"), str(control), "swap" if swap else "none"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, env=_child_environment())
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        pytest.fail("owned child exceeded deadline")
    assert process.returncode == 0, (process.returncode, stdout, stderr)
    assert stdout.strip() == b"closed"
    assert time.monotonic() - started < 3


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_fifo_with_well_formed_data_is_not_treated_as_a_marker(tmp_path):
    control = tmp_path / "synthetic-control"
    control.mkdir()
    marker = control / "failure_synthetic.json"
    os.mkfifo(marker)
    descriptor = os.open(marker, os.O_RDWR | os.O_NONBLOCK)
    os.write(descriptor, b'{"code":"artifact_storage_failed","timestamp":"2026-01-01T00:00:00+00:00"}')
    script = """
import os, signal, sys
from pathlib import Path
signal.alarm(3)
from round_robin.operations import durability
assert Path(durability.__file__).resolve() == Path(os.environ['ROUND_ROBIN_TEST_DURABILITY_FILE']).resolve()
_markers = durability._markers
try:
    _markers(Path(sys.argv[1]))
except ValueError:
    print('rejected', flush=True)
else:
    print('accepted', flush=True)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(control)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, env=_child_environment())
    try:
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail("owned child exceeded deadline")
    finally:
        os.close(descriptor)
    assert process.returncode == 0, (process.returncode, stdout, stderr)
    assert stdout.strip() == b"rejected"


def test_oversized_marker_with_valid_prefix_never_clears_gate(tmp_path):
    from round_robin.operations.durability import _markers, marker_pending
    control = tmp_path / "synthetic-control"
    control.mkdir()
    valid = json.dumps({"code": "artifact_storage_failed", "timestamp": "2026-01-01T00:00:00+00:00"})
    (control / "failure_synthetic.json").write_text(valid + " " * 1200 + "corrupt trailing data")
    assert marker_pending(tmp_path / "artifacts", control_directory=control)
    with pytest.raises(ValueError):
        _markers(control)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Platform lacks atomic no-follow opens")
def test_regular_marker_and_invalid_symlink_controls(tmp_path):
    from round_robin.operations.durability import _markers, marker_pending
    control = tmp_path / "synthetic-control"
    control.mkdir()
    marker = control / "failure_synthetic.json"
    content = {"code": "backup_failed", "timestamp": "2026-01-01T00:00:00+00:00"}
    marker.write_text(json.dumps(content))
    assert _markers(control) == [(marker, content)]
    assert marker_pending(tmp_path / "artifacts", control_directory=control)
    marker.unlink()
    target = tmp_path / "private-target"
    target.write_text(json.dumps(content))
    marker.symlink_to(target)
    assert marker_pending(tmp_path / "artifacts", control_directory=control)
    with pytest.raises(ValueError):
        _markers(control)


@pytest.mark.parametrize("size", [1024, 1025])
def test_marker_byte_limit_is_exact_and_closed(tmp_path, size):
    from round_robin.operations.durability import _markers, marker_pending
    control = tmp_path / "synthetic-control"
    control.mkdir()
    content = {"code": "backup_failed", "timestamp": "2026-01-01T00:00:00+00:00"}
    valid = json.dumps(content).encode()
    marker = control / "failure_synthetic.json"
    marker.write_bytes(valid + b" " * (size - len(valid)))
    assert marker_pending(tmp_path / "artifacts", control_directory=control)
    if size == 1024:
        assert _markers(control) == [(marker, content)]
    else:
        with pytest.raises(ValueError):
            _markers(control)


def test_descriptor_closed_if_opened_marker_cannot_be_inspected(tmp_path, monkeypatch):
    from round_robin.operations import durability
    control = tmp_path / "synthetic-control"
    control.mkdir()
    (control / "failure_synthetic.json").write_text('{}')
    real_open, real_fstat = os.open, os.fstat
    opened = []
    def tracked_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        opened.append(fd)
        return fd
    def fail_stat(fd):
        if fd in opened:
            raise OSError("synthetic error")
        return real_fstat(fd)
    with monkeypatch.context() as patch:
        patch.setattr(durability.os, "open", tracked_open)
        patch.setattr(durability.os, "fstat", fail_stat)
        assert durability.marker_pending(tmp_path / "artifacts", control_directory=control)
    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])
