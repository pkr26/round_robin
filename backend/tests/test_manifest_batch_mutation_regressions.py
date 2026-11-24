"""Deletion-manifest cursors must never skip records or block maintenance."""

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from round_robin.operations.recovery import _manifest_batch


def record(meeting):
    return {"meeting_id": meeting, "owner_id": "owner_one", "execution_fence": 1}


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def test_manifest_batch_resumes_without_replaying_or_skipping_records(tmp_path):
    path = tmp_path / "manifest.jsonl"
    first, second = record("meeting_first"), record("meeting_second")
    raw = encoded(first) + encoded(second)
    path.write_bytes(raw)
    records, cursor = _manifest_batch(path, None, 1)
    assert records == [first]
    assert cursor["offset"] == len(encoded(first))
    assert cursor["anchor"] == hashlib.sha256(raw[:cursor["offset"]]).hexdigest()
    records, final = _manifest_batch(path, cursor, 1)
    assert records == [second]
    assert final["offset"] == len(raw)
    assert _manifest_batch(path, final, 1)[0] == []


def test_same_content_replacement_with_new_inode_restarts_from_first_record(tmp_path):
    path = tmp_path / "manifest.jsonl"
    first, second = record("meeting_first"), record("meeting_second")
    raw = encoded(first) + encoded(second)
    path.write_bytes(raw)
    _, cursor = _manifest_batch(path, None, 1)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(raw)
    assert replacement.stat().st_ino != path.stat().st_ino
    os.replace(replacement, path)
    assert _manifest_batch(path, cursor, 1)[0] == [first]


@pytest.mark.parametrize("offset", [True, "1", -1, 999999])
def test_invalid_cursor_offsets_restart_from_first_record(tmp_path, offset):
    path = tmp_path / "manifest.jsonl"
    first = record("meeting_first")
    raw = encoded(first) + encoded(record("meeting_second"))
    path.write_bytes(raw)
    _, valid = _manifest_batch(path, None, 1)
    cursor = {"identity": valid["identity"], "offset": offset, "anchor": hashlib.sha256(raw[:1]).hexdigest()}
    assert _manifest_batch(path, cursor, 1)[0] == [first]


def test_cursor_without_offset_restarts_from_first_record(tmp_path):
    path = tmp_path / "manifest.jsonl"
    first = record("meeting_first")
    path.write_bytes(encoded(first))
    _, valid = _manifest_batch(path, None, 1)
    assert _manifest_batch(path, {"identity": valid["identity"]}, 1)[0] == [first]
    malformed = {"identity": valid["identity"], "anchor": hashlib.sha256(path.read_bytes()[:1]).hexdigest()}
    assert _manifest_batch(path, malformed, 1)[0] == [first]


def test_maximum_record_length_is_inclusive(tmp_path):
    path = tmp_path / "manifest.jsonl"
    value = record("meeting_first")
    body = json.dumps(value, separators=(",", ":")).encode()
    path.write_bytes(body + b" " * (1023 - len(body)) + b"\n")
    assert path.stat().st_size == 1024
    assert _manifest_batch(path, None, 1)[0] == [value]


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Platform does not provide O_NOFOLLOW")
def test_manifest_batch_refuses_symlinks(tmp_path):
    target = tmp_path / "actual.jsonl"
    target.write_bytes(encoded(record("meeting_first")))
    link = tmp_path / "manifest.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _manifest_batch(link, None, 1)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Platform does not support FIFOs")
def test_manifest_fifo_is_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "manifest.fifo"
    os.mkfifo(fifo)
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(root / "backend/src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
    code = '''import sys
from round_robin.operations.recovery import _manifest_batch, OperationError
try:
    _manifest_batch(sys.argv[1], None, 1)
except OperationError:
    raise SystemExit(0)
raise SystemExit(1)
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(fifo)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail("A FIFO manifest must be rejected without blocking")
    assert process.returncode == 0, (stdout, stderr)


def test_manifest_record_reads_remain_bounded(tmp_path, monkeypatch):
    path = tmp_path / "manifest.jsonl"
    first = record("meeting_first")
    path.write_bytes(encoded(first))
    original_fdopen = os.fdopen

    class BoundedReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def readline(self, size=-1):
            assert isinstance(size, int) and 0 <= size <= 2048, "A manifest record read must be bounded"
            return self.wrapped.readline(size)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", lambda descriptor, mode: BoundedReader(original_fdopen(descriptor, mode)))
        assert _manifest_batch(path, None, 1)[0] == [first]
