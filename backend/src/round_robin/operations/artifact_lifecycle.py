"""Coordinate database/file snapshots with deletion of referenced artifacts."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading

from sqlalchemy import event
from sqlalchemy.orm import Session

_local = threading.local()


@contextmanager
def artifact_lifecycle(directory: Path):
    """Cross-process lock, reentrant within one synchronous execution thread.

    Writers acquire DB policy locks first. Backups use plain snapshot reads
    here, releasing this guard before writing their status to the database.
    """
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    key = str(directory)
    if key not in held:
        descriptor = os.open(directory / ".artifact-lifecycle.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except BaseException:
            os.close(descriptor)
            raise
        held[key] = [descriptor, 0]
    held[key][1] += 1
    try:
        yield
    finally:
        held[key][1] -= 1
        if held[key][1] == 0:
            descriptor, _ = held.pop(key)
            os.close(descriptor)


def hold_artifact_lifecycle(session: Session, directory: Path):
    """Keep deletion fenced until its row changes commit or roll back."""
    if not session.in_transaction():
        raise RuntimeError("Artifact deletion requires an active transaction")
    key = str(Path(directory).resolve())
    held = session.info.setdefault("artifact_lifecycle_guards", {})
    if key not in held:
        guard = artifact_lifecycle(directory)
        guard.__enter__()
        held[key] = guard


@event.listens_for(Session, "after_transaction_end")
def _release_artifact_lifecycle(session, transaction):
    if transaction.parent is None:
        guards = session.info.pop("artifact_lifecycle_guards", {})
        for guard in reversed(list(guards.values())):
            guard.__exit__(None, None, None)
