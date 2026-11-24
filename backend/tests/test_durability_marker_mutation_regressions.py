"""Independent durability failures must not silently reopen dispatch or spending."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from round_robin.database.models import PolicyBucket
from round_robin.operations import durability
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

pytest_plugins = ("test_operations",)


class UnavailableDatabase:
    def __init__(self, control_directory=None):
        self.kw = {"info": {"durability_control_directory": control_directory}}

    def begin(self):
        raise SQLAlchemyError("SYNTHETIC_PRIVATE_DATABASE_ERROR")


def test_failure_reports_unsaved_when_both_marker_and_database_fail(tmp_path, monkeypatch, caplog):
    def fail_sync(_):
        raise OSError("SYNTHETIC_PRIVATE_STORAGE_ERROR")

    monkeypatch.setattr(durability, "_sync_directory", fail_sync)
    saved = durability.mark_failure(UnavailableDatabase(), "artifact_storage_failed", artifact_directory=tmp_path / "artifacts")
    assert saved is False
    assert "external operator quarantine" in caplog.text
    assert "SYNTHETIC_PRIVATE" not in caplog.text


def test_independent_marker_uses_factory_control_directory_when_database_fails(tmp_path):
    artifacts = tmp_path / "artifacts"
    control = tmp_path / "independent-control"
    saved = durability.mark_failure(UnavailableDatabase(control), "backup_failed", artifact_directory=artifacts)
    assert saved is True
    assert len(list(control.glob("failure_*.json"))) == 1
    assert durability.marker_pending(artifacts, control_directory=control)
    assert not list((tmp_path / "durability-control").glob("failure_*.json"))


@pytest.mark.parametrize("initially_locked", [False, True])
def test_repeated_failures_restore_only_the_preexisting_dispatch_state(prepared, tmp_path, initially_locked):
    app, _, _, _, settings = prepared
    factory = app.state.session_factory
    control = tmp_path / "operator-control"
    with factory.begin() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        policy.metadata_json = {**policy.metadata_json, "dispatch_locked": initially_locked}
        original_version = policy.version
    assert durability.mark_failure(factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory, control_directory=control)
    assert durability.mark_failure(factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory, control_directory=control)
    with factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked is True
        assert policy.version == original_version + 2
        assert policy.metadata_json["durability_compromised"] is True
        assert policy.metadata_json["dispatch_locked"] is True
        assert policy.metadata_json["durability_prior_dispatch_locked"] is initially_locked
        assert policy.metadata_json["durability_failure_code"] == "artifact_storage_failed"
        assert policy.metadata_json["backup_failure_unresolved"] is False
        assert datetime.fromisoformat(policy.metadata_json["durability_failed_at"]).tzinfo is not None
    durability.storage_check(factory, settings.artifact_directory, clear=True, control_directory=control)
    with factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked is True
        assert policy.metadata_json["dispatch_locked"] is initially_locked


def test_later_storage_failure_does_not_erase_unresolved_backup_failure(prepared, tmp_path):
    app, _, _, _, settings = prepared
    factory = app.state.session_factory
    control = tmp_path / "operator-control"
    durability.mark_failure(factory, "backup_failed", artifact_directory=settings.artifact_directory, control_directory=control)
    durability.mark_failure(factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory, control_directory=control)
    with factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.metadata_json["backup_failure_unresolved"] is True
        assert policy.metadata_json["durability_failure_code"] == "artifact_storage_failed"
    with pytest.raises(ValueError, match="verified backup"):
        durability.storage_check(factory, settings.artifact_directory, clear=True, control_directory=control)


def test_marker_creation_never_follows_existing_pending_symlink(prepared, tmp_path, monkeypatch):
    app, _, _, _, settings = prepared
    control = tmp_path / "operator-control"
    control.mkdir()
    victim = tmp_path / "untouched"
    victim.write_bytes(b"synthetic original")
    (control / ".pending_fixed").symlink_to(victim)
    monkeypatch.setattr(durability, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    assert durability.mark_failure(app.state.session_factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory, control_directory=control)
    assert victim.read_bytes() == b"synthetic original"
    with app.state.session_factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        assert policy.spending_locked is True and policy.metadata_json["dispatch_locked"] is True


def test_successful_marker_write_does_not_raise_false_critical_alarm(prepared, tmp_path, caplog):
    import logging

    app, _, _, _, settings = prepared
    control = tmp_path / "operator-control"
    with caplog.at_level(logging.CRITICAL, logger=durability.__name__):
        assert durability.mark_failure(app.state.session_factory, "artifact_storage_failed", artifact_directory=settings.artifact_directory, control_directory=control)
    assert len(list(control.glob("failure_*.json"))) == 1
    assert not any(record.levelno >= logging.CRITICAL for record in caplog.records)
