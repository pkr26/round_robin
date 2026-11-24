import os
import stat

import pytest
from round_robin.reports import storage
from round_robin.reports.storage import ArtifactStore


@pytest.mark.parametrize(
    ("fail_sync", "expected"),
    [(True, "original directory fsync failure"), (False, "directory close failure")],
)
def test_directory_descriptor_close_preserves_primary_error_and_happens_once(tmp_path, monkeypatch, fail_sync, expected):
    store = ArtifactStore(tmp_path / "artifacts")
    original_fsync, original_close = os.fsync, os.close
    directory_close_calls = []

    def sync(fd):
        if fail_sync and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("original directory fsync failure")
        return original_fsync(fd)

    def close(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        original_close(fd)
        if directory:
            directory_close_calls.append(fd)
            raise OSError("directory close failure")

    monkeypatch.setattr(storage.os, "fsync", sync)
    monkeypatch.setattr(storage.os, "close", close)
    with pytest.raises(OSError, match=expected):
        store.put(b"private content")
    assert len(directory_close_calls) == 1
    assert not list(store.directory.glob("export_*.bin*"))
