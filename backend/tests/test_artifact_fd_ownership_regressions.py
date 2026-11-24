import errno
import os
import stat
from pathlib import Path

import pytest
from round_robin.reports import storage
from round_robin.reports.storage import ArtifactStore


def test_failed_stream_construction_closes_new_file_descriptor(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    original = os.open
    descriptors = []

    def capture(path, flags, *args, **kwargs):
        fd = original(path, flags, *args, **kwargs)
        if str(path).endswith(".pending"):
            descriptors.append(fd)
        return fd

    def refuse_stream(*args, **kwargs):
        raise OSError("synthetic stream construction failure")

    monkeypatch.setattr(storage.os, "open", capture)
    monkeypatch.setattr(storage.os, "fdopen", refuse_stream)
    with pytest.raises(OSError, match="synthetic stream construction failure"):
        store.put(b"private content")
    assert len(descriptors) == 1
    fd = descriptors[0]
    try:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    assert not list(store.directory.glob("export_*.bin*"))


@pytest.mark.parametrize("fail_sync", [False, True])
def test_stream_owns_descriptor_after_successful_fdopen(tmp_path, monkeypatch, fail_sync):
    store = ArtifactStore(tmp_path / "artifacts")
    original_fdopen, original_close, original_fsync = os.fdopen, os.close, os.fsync
    streams = []
    manual_regular_file_closes = []

    def record_stream(fd, mode):
        stream = original_fdopen(fd, mode)
        streams.append(stream)
        return stream

    def track_close(fd):
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                manual_regular_file_closes.append(fd)
        except OSError:
            manual_regular_file_closes.append(fd)
        return original_close(fd)

    def sync(fd):
        if fail_sync and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("synthetic write failure")
        return original_fsync(fd)

    monkeypatch.setattr(storage.os, "fdopen", record_stream)
    monkeypatch.setattr(storage.os, "close", track_close)
    monkeypatch.setattr(storage.os, "fsync", sync)
    if fail_sync:
        with pytest.raises(OSError, match="synthetic write failure"):
            store.put(b"private content")
        assert not list(store.directory.glob("export_*.bin*"))
    else:
        key, _ = store.put(b"private content")
        assert (store.directory / key).read_bytes() == b"private content"
    assert len(streams) == 1 and streams[0].closed
    assert not manual_regular_file_closes


@pytest.mark.parametrize("cleanup_error", [OSError, RuntimeError, KeyboardInterrupt])
def test_cleanup_errors_do_not_replace_stream_construction_failure(tmp_path, monkeypatch, cleanup_error):
    store = ArtifactStore(tmp_path / "artifacts")
    original_open, original_close, original_unlink = os.open, os.close, Path.unlink
    descriptors = []

    def capture(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if str(path).endswith(".pending"):
            descriptors.append(fd)
        return fd

    def refuse_stream(*args, **kwargs):
        raise OSError("original stream failure")

    def refuse_close(fd):
        if fd in descriptors:
            raise cleanup_error("cleanup close failure")
        return original_close(fd)

    def refuse_unlink(path, *args, **kwargs):
        if str(path).endswith(".pending"):
            raise cleanup_error("cleanup unlink failure")
        return original_unlink(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage.os, "open", capture)
            patch.setattr(storage.os, "fdopen", refuse_stream)
            patch.setattr(storage.os, "close", refuse_close)
            patch.setattr(Path, "unlink", refuse_unlink)
            with pytest.raises(OSError, match="original stream failure"):
                store.put(b"private content")
    finally:
        for fd in descriptors:
            try:
                original_close(fd)
            except OSError:
                pass
    assert len(list(store.directory.glob("*.pending"))) == 1


def test_successful_rename_does_not_depend_on_unlinking_absent_temporary_path(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    original_unlink = Path.unlink

    def refuse_unlink(path, *args, **kwargs):
        if str(path).endswith(".pending"):
            raise PermissionError("unnecessary temporary cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    key, digest = store.put(b"private content")
    assert store.read(key, digest) == b"private content"


def test_unlink_failure_preserves_the_directory_sync_failure(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    original_fsync, original_unlink = os.fsync, Path.unlink

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("original directory sync failure")
        return original_fsync(fd)

    def refuse_unlink(path, *args, **kwargs):
        if str(path).endswith(".bin"):
            raise PermissionError("cleanup unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "fsync", sync)
    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    with pytest.raises(OSError, match="original directory sync failure"):
        store.put(b"private content")
    assert len(list(store.directory.glob("*.bin"))) == 1
