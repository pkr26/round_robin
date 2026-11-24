"""Clearing a storage quarantine requires capacity and every retained hash."""

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from round_robin.database.models import ExportRecord
from round_robin.database.services import lock_policies
from round_robin.database.visual_models import DesignVersion
from round_robin.operations import durability
from round_robin.reports.storage import ArtifactStore

from backend.tests.test_image_worker import seed_image

pytest_plugins = ("backend.tests.test_image_provider", "backend.tests.test_image_worker")


@pytest.mark.parametrize("free", [64 * 1024 * 1024 - 1, 64 * 1024 * 1024])
def test_storage_clear_requires_at_least_64_mib(image_factory, image_configuration, tmp_path, monkeypatch, free):
    seed_image(image_factory, image_configuration)
    monkeypatch.setattr(durability.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    artifact_dir = tmp_path / "artifacts"
    control = tmp_path / "control"
    if free < 64 * 1024 * 1024:
        with pytest.raises(ValueError):
            durability.storage_check(image_factory, artifact_dir, clear=True, control_directory=control)
    else:
        result = durability.storage_check(image_factory, artifact_dir, clear=True, control_directory=control)
        assert result["durability_gate_cleared"] is True and result["spending_unlocked"] is False


def test_storage_probe_is_private_and_created_exclusively(image_factory, image_configuration, tmp_path, monkeypatch):
    seed_image(image_factory, image_configuration)
    actual_open = os.open
    observed = []
    artifact_dir = tmp_path / "artifacts"

    def capture_open(path, flags, *args, **kwargs):
        fd = actual_open(path, flags, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and Path(path).parent.resolve() == artifact_dir.resolve():
            observed.append((flags, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd

    monkeypatch.setattr(durability.shutil, "disk_usage", lambda path: SimpleNamespace(free=64 * 1024 * 1024))
    monkeypatch.setattr(durability.os, "open", capture_open)
    durability.storage_check(image_factory, artifact_dir, control_directory=tmp_path / "control")
    assert len(observed) == 1
    flags, mode = observed[0]
    assert flags & os.O_CREAT and flags & os.O_EXCL
    assert mode & 0o177 == 0  # No group/world access or owner execute bit.


@pytest.mark.parametrize("damaged", ["export", "canonical", "svg", "source"])
def test_storage_quarantine_stays_closed_for_any_corrupt_retained_asset(
    image_factory, image_configuration, tmp_path, monkeypatch, damaged
):
    design_id, _ = seed_image(image_factory, image_configuration)
    artifact_dir = tmp_path / "artifacts"
    store = ArtifactStore(artifact_dir)
    export_key, export_hash = store.put(b"synthetic retained report")
    export_key_two, export_hash_two = store.put(b"another retained report")
    root = artifact_dir / "visual" / "owner_image" / design_id
    root.mkdir(parents=True)
    contents = {"canonical": b"synthetic raster", "svg": b"<svg/>", "source": b'{"nodes":[]}'}
    paths = {name: root / name for name in contents}
    for name, path in paths.items():
        path.write_bytes(contents[name])
    digest = lambda name: hashlib.sha256(contents[name]).hexdigest()
    with image_factory.begin() as session:
        session.add_all([
            ExportRecord(id="export_one", run_id="synthetic_run", owner_id="owner_image", report_version=1,
                         format="json", status="ready", path=export_key, sha256=export_hash),
            ExportRecord(id="export_two", run_id="synthetic_run", owner_id="owner_image", report_version=1,
                         format="json", status="ready", path=export_key_two, sha256=export_hash_two),
            DesignVersion(id="version_one", session_id=design_id, version=1, canonical_hash=digest("canonical"),
                          original_hash=digest("canonical"), asset_path=str(paths["canonical"]), width=64, height=64,
                          source_kind="synthetic", provenance={"svg_path": str(paths["svg"]), "svg_hash": digest("svg"),
                                                               "source_path": str(paths["source"]), "source_hash": digest("source")}),
        ])
        policy, _ = lock_policies(session, "owner_operator")
        policy.spending_locked = True
        policy.metadata_json = {**policy.metadata_json, "durability_compromised": True, "dispatch_locked": True,
                                "durability_prior_dispatch_locked": False}
    control = tmp_path / "control"
    control.mkdir()
    marker = control / "failure_test.json"
    marker.write_text('{"code":"artifact_storage_failed","timestamp":"2026-01-01T00:00:00+00:00"}')
    monkeypatch.setattr(durability.shutil, "disk_usage", lambda path: SimpleNamespace(free=64 * 1024 * 1024))
    path = store.directory / export_key if damaged == "export" else paths[damaged]
    original = path.read_bytes()
    path.write_bytes(b"synthetic corrupted bytes")
    with pytest.raises(ValueError):
        durability.storage_check(image_factory, artifact_dir, clear=True, control_directory=control)
    assert marker.exists()
    with image_factory() as session:
        policy, _ = lock_policies(session, "owner_operator")
        assert policy.spending_locked is True and policy.metadata_json["durability_compromised"] is True
    path.write_bytes(original)
    result = durability.storage_check(image_factory, artifact_dir, clear=True, control_directory=control)
    assert result["retained_artifacts_verified"] == 3 and result["spending_unlocked"] is False
    assert not marker.exists()
    with image_factory() as session:
        policy, _ = lock_policies(session, "owner_operator")
        assert policy.spending_locked is True and policy.metadata_json["durability_compromised"] is False
