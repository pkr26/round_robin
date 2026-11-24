"""Persist a closed write/dispatch gate after storage or backup failure."""
import logging
import json
from contextlib import contextmanager
from datetime import datetime
import fcntl
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from round_robin.database.models import ExportRecord, utcnow
from round_robin.database.services import lock_policies
from round_robin.reports.storage import ArtifactStore


def control_path(artifact_directory: Path, control_directory: Path | None = None) -> Path:
    configured = control_directory or os.environ.get("ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY")
    return Path(configured) if configured else Path(artifact_directory).parent / "durability-control"


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _marker_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_directory(directory.parent)
    descriptor = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _markers(directory):
    try:
        try:
            with os.scandir(directory) as items:
                files = [directory / item.name for item in items if item.name.startswith("failure_") and item.name.endswith(".json")]
        except FileNotFoundError:
            files = []
        if len(files) > 10000:
            raise ValueError("Durability marker count requires operator review")
        entries = []
        for path in files:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("Durability marker requires operator review")
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = None
                    raw = stream.read(1025)
                if len(raw) > 1024:
                    raise ValueError("Durability marker requires operator review")
                data = json.loads(raw)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            if set(data) != {"code", "timestamp"} or data["code"] not in {"artifact_storage_failed", "backup_failed"}:
                raise ValueError("Durability marker requires operator review")
            datetime.fromisoformat(data["timestamp"])
            entries.append((path, data))
        return entries
    except OSError:
        raise ValueError("Durability control storage could not be inspected") from None


def marker_pending(artifact_directory=None, *, control_directory=None, session=None):
    if artifact_directory is None:
        from round_robin.configuration import settings
        artifact_directory = session.info.get("artifact_directory", settings.artifact_directory) if session is not None else settings.artifact_directory
    if session is not None:
        control_directory = control_directory or session.info.get("durability_control_directory")
    try:
        return bool(_markers(control_path(artifact_directory, control_directory)))
    except (ValueError, OSError, TypeError, KeyError):
        return True


def mark_failure(factory, code, *, artifact_directory: Path, control_directory: Path | None = None):
    if code not in {"artifact_storage_failed", "backup_failed"}:
        raise ValueError("Unsupported durability failure code")
    control_directory = control_directory or getattr(factory, "kw", {}).get("info", {}).get("durability_control_directory")
    marker_saved = False
    try:
        directory = control_path(artifact_directory, control_directory)
        with _marker_lock(directory):
            temporary = directory / (".pending_" + uuid4().hex)
            final = directory / ("failure_" + uuid4().hex + ".json")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(json.dumps({"code": code, "timestamp": utcnow().isoformat()}).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, final)
                _sync_directory(directory)
                marker_saved = True
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError):
        logging.getLogger(__name__).critical("Independent durability marker unavailable; external operator quarantine is required if database recording also fails")
    try:
        with factory.begin() as session:
            policy, _ = lock_policies(session, "owner_operator")
            data = policy.metadata_json
            prior = data.get("durability_prior_dispatch_locked", False) if data.get("durability_compromised") else data.get("dispatch_locked", False)
            policy.spending_locked = True
            policy.metadata_json = {**data, "durability_compromised": True, "dispatch_locked": True, "durability_prior_dispatch_locked": prior, "durability_failure_code": code, "durability_failed_at": utcnow().isoformat(), "backup_failure_unresolved": code == "backup_failed" or data.get("backup_failure_unresolved", False)}
            policy.version += 1
    except SQLAlchemyError:
        # The independent marker survives DB recovery. If neither store can
        # persist, services require external operator quarantine.
        logging.getLogger(__name__).error("Durability failure could not be recorded; database writes and dispatch must remain closed")
        return marker_saved
    logging.getLogger(__name__).error("Durability compromised; writes and dispatch locked pending operator storage check")
    return True


def record_backup(factory, snapshot_hash):
    with factory.begin() as session:
        policy, _ = lock_policies(session, "owner_operator")
        policy.metadata_json = {**policy.metadata_json, "last_verified_backup_at": utcnow().isoformat(), "last_verified_backup_hash": snapshot_hash, "backup_failure_unresolved": False}


def storage_check(factory, artifact_directory: Path, *, clear=False, control_directory: Path | None = None):
    directory = control_path(artifact_directory, control_directory)
    with _marker_lock(directory):
        return _storage_check(factory, artifact_directory, clear=clear, directory=directory)


def _storage_check(factory, artifact_directory: Path, *, clear, directory):
    """Probe fsynced private writes and validate every retained ready artifact.

    Clearing this gate is an explicit operator action. It never unlocks spending,
    restoration quarantine, a pre-existing dispatch lock or the drain setting.
    """
    markers = _markers(directory)
    store = ArtifactStore(artifact_directory)
    if shutil.disk_usage(store.directory).free < 64 * 1024 * 1024:
        raise ValueError("At least 64 MiB of free artifact storage is required")
    probe = store.directory / (".durability_probe_" + uuid4().hex)
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"Round Robin private storage durability probe\n")
            stream.flush()
            os.fsync(stream.fileno())
        probe_directory_fd = os.open(store.directory, os.O_RDONLY)
        try:
            os.fsync(probe_directory_fd)
            probe.unlink()
            os.fsync(probe_directory_fd)
        finally:
            os.close(probe_directory_fd)
    finally:
        probe.unlink(missing_ok=True)
    checked = 0
    with factory.begin() as session:
        policy, _ = lock_policies(session, "owner_operator")
        from round_robin.database.visual_models import DesignSession, DesignVersion
        from round_robin.visual.service import owned_asset_bytes
        for artifact in session.scalars(select(ExportRecord).where(ExportRecord.status == "ready")):
            store.read(artifact.path, artifact.sha256)
            checked += 1
        for version in session.scalars(select(DesignVersion)):
            design = session.get(DesignSession, version.session_id)
            owned_asset_bytes(version, design, artifact_directory)
            for source, selector in (("svg_path", True), ("source_path", "source")):
                if version.provenance.get(source):
                    owned_asset_bytes(version, design, artifact_directory, native_source=selector)
            checked += 1
        data = policy.metadata_json
        if markers:
            policy.spending_locked = True
        if clear:
            if data.get("restore_quarantine"):
                raise ValueError("Storage checks cannot clear restoration quarantine")
            backup_failures = [item["timestamp"] for _, item in markers if item["code"] == "backup_failed"]
            verified = data.get("last_verified_backup_at")
            if data.get("backup_failure_unresolved") or (backup_failures and (not verified or max(backup_failures) >= verified)):
                raise ValueError("Create a new verified backup before clearing a backup failure")
            policy.metadata_json = {**{key: value for key, value in data.items() if key != "durability_prior_dispatch_locked"}, "durability_compromised": False, "dispatch_locked": bool(data.get("durability_prior_dispatch_locked", data.get("dispatch_locked", False))), "storage_checked_at": utcnow().isoformat()}
            policy.version += 1
        else:
            policy.metadata_json = {**data, "storage_checked_at": utcnow().isoformat()}
    if clear:
        # Commit database clear before removing independent markers. The file
        # lock prevents erasing a concurrent newly recorded failure.
        for path, _ in markers:
            path.unlink()
        _sync_directory(directory)
    return {"status": "storage_checked", "retained_artifacts_verified": checked, "durability_gate_cleared": clear, "spending_unlocked": False, "provider_requests": 0}
