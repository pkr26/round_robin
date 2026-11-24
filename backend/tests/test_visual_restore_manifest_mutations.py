"""Visual restore refuses malformed manifests before any file can be copied."""

from copy import deepcopy
from pathlib import Path

import pytest
from round_robin.visual import service
from round_robin.visual.iterations import VisualLimit


def item(*, digest="a" * 64, suffix=".png"):
    return {"key": f"visual_assets/{digest}{suffix}", "relative_path": f"visual/owner/session/copy{suffix}",
            "sha256": digest, "original_path": f"original{suffix}"}


@pytest.fixture
def restore(tmp_path, monkeypatch):
    backup, target = tmp_path / "backup", tmp_path / "target"
    backup.mkdir()
    target.mkdir()
    copies = []
    monkeypatch.setattr(service, "_private_copy", lambda *args: copies.append(args))
    return backup, target, copies


@pytest.mark.parametrize("invalid", ["absolute_key", "absolute_target", "key_traversal", "target_traversal", "key_namespace", "extension", "hash_name", "empty_key", "empty_target"])
def test_visual_restore_rejects_unsafe_or_unbound_manifest_before_copy(restore, invalid):
    backup, target, copies = restore
    entry = item()
    if invalid == "absolute_key":
        entry["key"] = str(backup / entry["key"])
    elif invalid == "absolute_target":
        entry["relative_path"] = str(target / entry["relative_path"])
    elif invalid == "key_traversal":
        entry["key"] = f"visual_assets/nested/../{entry['sha256']}.png"
    elif invalid == "target_traversal":
        entry["relative_path"] = "visual/unused/../owner/session/copy.png"
    elif invalid == "key_namespace":
        entry["key"] = f"other/{entry['sha256']}.png"
    elif invalid == "extension":
        entry["key"] = f"visual_assets/{entry['sha256']}.bmp"
        entry["relative_path"] = "visual/owner/session/copy.bmp"
    elif invalid == "hash_name":
        entry["key"] = f"visual_assets/{'b' * 64}.png"
    elif invalid == "empty_key":
        entry["key"] = ""
    else:
        entry["relative_path"] = ""
    with pytest.raises(VisualLimit):
        service.restore_visual_assets(backup, target, [entry], {})
    assert copies == []


def test_visual_restore_refuses_even_an_in_backup_symlink_before_copy(restore):
    backup, target, copies = restore
    entry = item()
    directory = backup / "visual_assets"
    directory.mkdir()
    inside = backup / "synthetic-data"
    inside.write_bytes(b"synthetic")
    (backup / entry["key"]).symlink_to(inside)
    with pytest.raises(VisualLimit):
        service.restore_visual_assets(backup, target, [entry], {})
    assert copies == [] and inside.read_bytes() == b"synthetic"


@pytest.mark.parametrize("linked_parent", ["source", "target"])
def test_visual_restore_rejects_parent_link_escaping_its_namespace_before_copy(restore, linked_parent, tmp_path):
    backup, target, copies = restore
    entry = item()
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked_parent == "source":
        (backup / "visual_assets").mkdir()
        (backup / "visual_assets" / "link").symlink_to(outside, target_is_directory=True)
        entry["key"] = f"visual_assets/link/{entry['sha256']}.png"
    else:
        (target / "visual").mkdir()
        (target / "visual" / "link").symlink_to(outside, target_is_directory=True)
        entry["relative_path"] = "visual/link/copy.png"
    with pytest.raises(VisualLimit):
        service.restore_visual_assets(backup, target, [entry], {})
    assert copies == []


def test_visual_restore_rebases_each_native_source_and_enforces_type_limits(restore):
    backup, target, copies = restore
    entries = [item(digest=character * 64, suffix=suffix) for character, suffix in
               [("a", ".png"), ("b", ".svg"), ("c", ".json"), ("d", ".html")]]
    tables = {"design_versions": [{"asset_path": "original.png", "provenance": {"svg_path": "original.svg", "source_path": "original.json"}}]}
    original = deepcopy(entries)
    service.restore_visual_assets(backup, target, entries, tables)
    assert entries == original and len(copies) == 4
    assert [call[3] for call in copies] == [64 * 1024 * 1024, 1024 * 1024, 1024 * 1024, 1024 * 1024]
    row = tables["design_versions"][0]
    assert row["asset_path"] == str(target / entries[0]["relative_path"])
    assert row["provenance"]["svg_path"] == str(target / entries[1]["relative_path"])
    assert row["provenance"]["source_path"] == str(target / entries[2]["relative_path"])
    assert all(Path(call[0]).is_relative_to(backup) and Path(call[1]).is_relative_to(target / "visual") for call in copies)


def test_visual_restore_accepts_legacy_row_without_native_provenance(restore):
    backup, target, copies = restore
    entry = item()
    row = {"asset_path": entry["original_path"]}
    service.restore_visual_assets(backup, target, [entry], {"design_versions": [row]})
    assert len(copies) == 1 and row["asset_path"] == str(target / entry["relative_path"])


@pytest.mark.parametrize("missing", ["canonical", "svg", "source"])
def test_visual_restore_rejects_missing_referenced_asset(restore, missing):
    backup, target, _ = restore
    entries = [item(digest="a" * 64, suffix=".png"), item(digest="b" * 64, suffix=".svg"), item(digest="c" * 64, suffix=".json")]
    row = {"asset_path": "original.png", "provenance": {"svg_path": "original.svg", "source_path": "original.json"}}
    missing_path = {"canonical": "original.png", "svg": "original.svg", "source": "original.json"}[missing]
    entries = [entry for entry in entries if entry["original_path"] != missing_path]
    with pytest.raises(VisualLimit):
        service.restore_visual_assets(backup, target, entries, {"design_versions": [row]})


@pytest.mark.parametrize("count", [2000, 2001])
def test_visual_restore_manifest_count_limit(restore, count):
    backup, target, copies = restore
    entries = [item()] * count
    if count == 2001:
        with pytest.raises(VisualLimit):
            service.restore_visual_assets(backup, target, entries, {})
        assert copies == []
    else:
        service.restore_visual_assets(backup, target, entries, {})
        assert len(copies) == count
