"""Exclusive, append-only, hash-linked journal for the offline experiment runner.

An authorization is durable before the simulated adapter is called. An interrupted
authorization is treated as possibly sent. Hashes detect corruption, not a hostile
operator rewriting the complete journal; this is not a signed accounting service.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any


class JournalError(RuntimeError):
    pass


class JournalBusy(JournalError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def private_new_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


class Journal:
    """One aggregate journal directory, reused for every batch in this simulation."""

    def __init__(self, path: str | Path, *, mode: str = "simulation_only"):
        if mode not in {"simulation_only", "explicit_live_study"}:
            raise JournalError("unsupported journal execution profile")
        supplied = Path(path)
        if supplied.is_symlink():
            raise JournalError("journal directory cannot be a symlink")
        self.path = supplied.resolve()
        self.mode = mode
        self.events: list[dict[str, Any]] = []
        self._lock = None
        self._stream = None
        self._digest = "0" * 64

    def __enter__(self) -> Journal:
        created = False
        missing_directories = []
        candidate = self.path
        while not candidate.exists():
            missing_directories.append(candidate)
            candidate = candidate.parent
        created_directories = []
        for directory in reversed(missing_directories):
            try:
                directory.mkdir(mode=0o700)
                created_directories.append(directory)
            except FileExistsError:
                if not directory.is_dir():
                    raise JournalError("journal path is not a directory") from None
        created = self.path in created_directories
        if not self.path.is_dir() or self.path.stat().st_uid != os.geteuid():
            raise JournalError("journal directory must be owned by the current operator")
        if self.mode == "explicit_live_study" or created:
            os.chmod(self.path, 0o700)
        lock_path = self.path / "lock"
        if lock_path.is_symlink():
            raise JournalError("journal lock cannot be a symlink")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise JournalError("journal lock must be a regular operator file")
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        self._lock = os.fdopen(descriptor, "a+b")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            self._lock = None
            raise JournalBusy("aggregate journal is already owned by another process") from None
        try:
            if created:
                # Persist each newly created directory entry as well as the
                # files below it; fsync of the leaf alone cannot preserve its
                # own entry after a machine crash.
                for directory in created_directories:
                    parent_fd = os.open(directory.parent, os.O_RDONLY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
            manifest_path = self.path / "manifest.json"
            records_path = self.path / "events.jsonl"
            head_path = self.path / "head.json"
            if created:
                # Exclusive directory creation, then exclusive files. A crash at
                # any intermediate boundary leaves an unusable directory.
                with private_new_file(manifest_path) as stream:
                    stream.write(canonical({"schema_version": 1, "mode": self.mode}) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                with private_new_file(records_path) as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                with private_new_file(head_path) as stream:
                    stream.write(canonical({"sequence": 0, "hash": self._digest}) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                directory_fd = os.open(self.path, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            if not manifest_path.is_file() or not records_path.is_file() or not head_path.is_file():
                raise JournalError("existing journal is incomplete; refusing to recreate missing history")
            for owned_file in (manifest_path, records_path, head_path):
                if owned_file.is_symlink() or owned_file.stat().st_uid != os.geteuid():
                    raise JournalError("journal files must be owned regular operator files")
                if self.mode == "explicit_live_study":
                    os.chmod(owned_file, 0o600)
            try:
                manifest = json.loads(manifest_path.read_bytes())
            except (ValueError, OSError) as exc:
                raise JournalError("invalid journal manifest") from exc
            if manifest != {"schema_version": 1, "mode": self.mode} or type(manifest.get("schema_version")) is not int:
                raise JournalError("unsupported journal manifest")
            self.events = []
            self._digest = "0" * 64
            raw = records_path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                raise JournalError("truncated journal; manual investigation required")
            for line in raw.splitlines():
                try:
                    record = json.loads(line)
                    payload = {"sequence": len(self.events) + 1, "previous_hash": self._digest, "event": record["event"]}
                    digest = hashlib.sha256(canonical(payload)).hexdigest()
                    if set(record) != {"sequence", "previous_hash", "event", "hash"} or record != {**payload, "hash": digest} or type(record["sequence"]) is not int:
                        raise ValueError("invalid chain")
                    if not isinstance(record["event"], dict):
                        raise ValueError("invalid event")
                except (KeyError, TypeError, ValueError) as exc:
                    raise JournalError("journal integrity check failed") from exc
                self.events.append(record["event"])
                self._digest = digest
            try:
                head = json.loads(head_path.read_bytes())
            except (ValueError, OSError) as exc:
                raise JournalError("invalid durable journal head") from exc
            if head != {"sequence": len(self.events), "hash": self._digest} or type(head.get("sequence")) is not int or (self.path / "head.pending").exists():
                raise JournalError("journal/head mismatch; refusing truncated or interrupted history")
            self._stream = records_path.open("ab", buffering=0)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def append(self, event: dict[str, Any]) -> None:
        if self._stream is None:
            raise JournalError("journal is not exclusively open")
        if not isinstance(event, dict):
            raise JournalError("journal events must be objects")
        payload = {"sequence": len(self.events) + 1, "previous_hash": self._digest, "event": event}
        digest = hashlib.sha256(canonical(payload)).hexdigest()
        data = canonical({**payload, "hash": digest}) + b"\n"
        # Never reuse this instance after a partial write/fsync failure.
        try:
            written = self._stream.write(data)
            if written != len(data):
                raise OSError("short journal write")
            os.fsync(self._stream.fileno())
            # A separate durable checkpoint also detects truncation exactly at a
            # valid line boundary. A crash between these writes fails closed.
            pending = self.path / "head.pending"
            with private_new_file(pending) as stream:
                stream.write(canonical({"sequence": len(self.events) + 1, "hash": digest}) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.path / "head.json")
            directory_fd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        self.events.append(json.loads(canonical(event)))
        self._digest = digest

    def __exit__(self, *_: object) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None
