"""Temporary artifact writes preserve bounds, privacy and cleanup on failure."""

import hashlib
import os
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from round_robin.database.models import utcnow
from round_robin.operations.maintenance import maintenance
from round_robin.reports import storage
from round_robin.reports.storage import ArtifactStore

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_exact_ten_mib_limit_is_allowed_with_verified_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    content = b"x" * (10 * 1024 * 1024)
    key, digest = store.put(content)
    assert digest == hashlib.sha256(content).hexdigest()
    assert store.read(key, digest) == content


def test_new_file_is_private_even_with_a_permissive_process_umask(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    old = os.umask(0)
    try:
        key, _ = store.put(b"private data")
    finally:
        os.umask(old)
    assert stat.S_IMODE((store.directory / key).stat().st_mode) == 0o600


def test_exclusive_creation_works_when_nofollow_constant_is_unavailable(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.delattr(storage.os, "O_NOFOLLOW", raising=False)
    key, _ = store.put(b"private data")
    assert (store.directory / key).read_bytes() == b"private data"


def test_pending_symlink_cannot_redirect_or_overwrite_private_bytes(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(storage.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    other = tmp_path / "other"
    other.write_bytes(b"unrelated private bytes")
    (store.directory / ("export_" + "a" * 32 + ".bin.pending")).symlink_to(other)
    with pytest.raises(FileExistsError):
        store.put(b"replacement")
    assert other.read_bytes() == b"unrelated private bytes"


def test_unpublished_failure_does_not_remove_preexisting_final_file(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(storage.uuid, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    existing = store.directory / ("export_" + "b" * 32 + ".bin")
    existing.write_bytes(b"previous")
    monkeypatch.setattr(storage.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("file sync failed")))
    with pytest.raises(OSError, match="file sync failed"):
        store.put(b"replacement")
    assert existing.read_bytes() == b"previous"
    assert not list(store.directory.glob("*.pending"))


@pytest.mark.parametrize("remove_published", [False, True])
def test_directory_sync_failure_does_not_leave_unowned_private_bytes(tmp_path, monkeypatch, remove_published):
    store = ArtifactStore(tmp_path / "artifacts")
    original = os.fsync
    def inject(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            if remove_published:
                for path in store.directory.glob("*.bin"):
                    path.unlink()
            raise OSError("directory sync failed")
        return original(fd)
    monkeypatch.setattr(storage.os, "fsync", inject)
    with pytest.raises(OSError, match="directory sync failed"):
        store.put(b"private data")
    assert not list(store.directory.iterdir())


def test_interrupted_temporary_export_is_owned_by_aged_orphan_cleanup(factory, tmp_path):
    directory = tmp_path / "artifacts"
    script = """
import os, sys
from pathlib import Path
from round_robin.reports import storage
storage.os.replace = lambda *args: os._exit(19)
storage.ArtifactStore(Path(sys.argv[1])).put(b'private crash fixture')
"""
    env = {**os.environ, "PYTHONPATH": str(Path(storage.__file__).parents[2])}
    process = subprocess.Popen([sys.executable, "-c", script, str(directory)], env=env, start_new_session=True)
    try:
        assert process.wait(timeout=8) == 19
    finally:
        if process.poll() is None:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    result = maintenance(factory, directory, now=utcnow() + timedelta(hours=2))
    assert result["orphaned_export_files"] == 1
    assert not list(directory.glob("export_*.bin*"))
