"""Private immutable local artifact bytes with opaque server-selected keys."""

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path


class StorageFailure(ValueError):
    pass


class ArtifactStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-z]+_[a-f0-9]{32}\.bin", key):
            raise StorageFailure("Only server-issued artifact keys are supported")
        return self.directory / key

    def put(self, content: bytes, *, prefix="export") -> tuple[str, str]:
        if not re.fullmatch(r"[a-z]+", prefix) or len(content) > 10 * 1024 * 1024:
            raise StorageFailure("Artifact is outside storage bounds")
        key = f"{prefix}_{uuid.uuid4().hex}.bin"
        path = self._path(key)
        temporary = self.directory / (key + ".pending")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        published = False
        try:
            stream = os.fdopen(descriptor, "wb")
            descriptor = None  # The stream owns the descriptor after fdopen succeeds.
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            published = True
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            except BaseException:
                try:
                    os.close(directory_fd)
                except BaseException:
                    pass
                raise
            else:
                os.close(directory_fd)
        except BaseException:
            # Cleanup is best effort and cannot replace the failure the caller
            # needs to diagnose. A successfully created stream owns its fd.
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            if published:
                try:
                    path.unlink(missing_ok=True)
                except BaseException:
                    pass
            try:
                temporary.unlink(missing_ok=True)
            except BaseException:
                pass
            raise
        return key, hashlib.sha256(content).hexdigest()

    def read(self, key: str, expected_sha256: str) -> bytes:
        # Open nonblocking before inspecting the descriptor so a replaced FIFO
        # cannot hang a download or backup. Verify the same descriptor we read.
        descriptor = os.open(self._path(key), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 10 * 1024 * 1024:
                raise StorageFailure("Saved artifact failed integrity or size verification")
            stream = os.fdopen(descriptor, "rb")
            descriptor = -1  # The stream now owns the descriptor.
            with stream:
                content = stream.read(10 * 1024 * 1024 + 1)
        finally:
            if descriptor != -1:
                os.close(descriptor)
        if len(content) > 10 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != expected_sha256:
            raise StorageFailure("Saved artifact failed integrity or size verification")
        return content

    def purge(self, key: str) -> None:
        from round_robin.operations.artifact_lifecycle import artifact_lifecycle
        with artifact_lifecycle(self.directory):
            self._path(key).unlink(missing_ok=True)
