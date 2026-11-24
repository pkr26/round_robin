"""Visual backup copies retain private creation modes and exact byte bounds."""

import hashlib
import os
import stat
from pathlib import Path

import pytest
from round_robin.visual import service
from round_robin.visual.iterations import VisualLimit


def test_private_visual_copy_requests_private_exclusive_file_and_directory(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    source.write_bytes(b"synthetic private visual")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "visual_assets" / "synthetic-copy"
    original_open, original_mkdir = os.open, Path.mkdir
    file_requests, directory_requests = [], []

    def capture_open(path, flags, mode=0o777, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == target:
            file_requests.append((flags, mode))
        return original_open(path, flags, mode, **kwargs)

    def capture_mkdir(path, mode=0o777, parents=False, exist_ok=False):
        if path == target.parent:
            directory_requests.append(mode)
        return original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(service.os, "open", capture_open)
    monkeypatch.setattr(Path, "mkdir", capture_mkdir)
    service._private_copy(source, target, digest, 1024)
    assert target.read_bytes() == source.read_bytes()
    assert len(file_requests) == len(directory_requests) == 1
    flags, file_mode = file_requests[0]
    assert flags & os.O_CREAT and flags & os.O_EXCL
    assert file_mode & 0o177 == 0 and file_mode & 0o600 == 0o600
    directory_mode = directory_requests[0]
    assert directory_mode & 0o077 == 0 and directory_mode & 0o700 == 0o700
    assert stat.S_IMODE(target.stat().st_mode) & 0o177 == 0
    assert stat.S_IMODE(target.parent.stat().st_mode) & 0o077 == 0


def test_private_visual_read_accepts_exact_bound_and_rejects_overflow_and_bad_hash(tmp_path):
    path = tmp_path / "synthetic-source"
    path.write_bytes(b"abcd")
    digest = hashlib.sha256(b"abcd").hexdigest()
    assert service.checked_private_bytes(path, digest, 4) == b"abcd"
    with pytest.raises(VisualLimit):
        service.checked_private_bytes(path, digest, 3)
    with pytest.raises(VisualLimit):
        service.checked_private_bytes(path, "0" * 64, 4)
