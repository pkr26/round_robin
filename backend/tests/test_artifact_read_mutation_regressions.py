"""Saved artifact reads remain bounded and reject non-regular files."""

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from round_robin.reports.storage import ArtifactStore, StorageFailure

LIMIT = 10 * 1024 * 1024


def test_artifact_read_accepts_exact_limit_and_rejects_one_byte_over(tmp_path):
    store = ArtifactStore(tmp_path)
    data = b"x" * LIMIT
    key, digest = store.put(data)
    assert store.read(key, digest) == data
    oversized = data + b"x"
    (store.directory / key).write_bytes(oversized)
    with pytest.raises(StorageFailure):
        store.read(key, hashlib.sha256(oversized).hexdigest())


def test_artifact_read_never_waits_for_fifo_writer(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are unavailable")
    store = ArtifactStore(tmp_path)
    key, digest = store.put(b"saved")
    (store.directory / key).unlink()
    os.mkfifo(store.directory / key)
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "backend/src")
    code = """
import sys
from pathlib import Path
from round_robin.reports.storage import ArtifactStore, StorageFailure
try:
    ArtifactStore(Path(sys.argv[1])).read(sys.argv[2], sys.argv[3])
except StorageFailure:
    pass
else:
    raise AssertionError('non-regular artifact accepted')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(store.directory), key, digest],
        cwd=root, env=environment, capture_output=True, text=True, timeout=8, check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("failed_operation", ["fstat", "fdopen"])
def test_artifact_read_closes_descriptor_when_preparation_fails(tmp_path, monkeypatch, failed_operation):
    store = ArtifactStore(tmp_path)
    key, digest = store.put(b"saved")
    opened = []

    def fail(descriptor, *args, **kwargs):
        opened.append(descriptor)
        raise OSError("synthetic preparation failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, failed_operation, fail)
        with pytest.raises(OSError, match="synthetic preparation failure"):
            store.read(key, digest)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_artifact_read_keeps_a_size_cap_if_the_file_grows_after_stat(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    key, _ = store.put(b"saved")
    content = b"x" * (LIMIT + 2)
    (store.directory / key).write_bytes(content)
    digest = hashlib.sha256(content[:LIMIT + 1]).hexdigest()
    actual_fstat = os.fstat
    actual_fdopen = os.fdopen
    requested = []

    def size_before_growth(descriptor):
        fields = list(actual_fstat(descriptor))
        fields[stat.ST_SIZE] = 0
        return os.stat_result(fields)

    class ReadProbe:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            requested.append(size)
            return self.stream.read(size)

    def observe_fdopen(descriptor, *args, **kwargs):
        return ReadProbe(actual_fdopen(descriptor, *args, **kwargs))

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", size_before_growth)
        patch.setattr(os, "fdopen", observe_fdopen)
        with pytest.raises(StorageFailure):
            store.read(key, digest)
    assert len(requested) == 1 and isinstance(requested[0], int) and 0 <= requested[0] <= LIMIT + 1
