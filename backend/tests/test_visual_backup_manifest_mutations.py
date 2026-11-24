"""Visual backup manifests retain every unique private asset and its bound."""

import hashlib
from pathlib import Path

import pytest
from round_robin.visual import service
from round_robin.visual.iterations import VisualLimit


@pytest.fixture
def backup(tmp_path, monkeypatch):
    root, destination = tmp_path / "artifacts", tmp_path / "backup"
    private = root / "visual" / "owner" / "session"
    private.mkdir(parents=True)
    destination.mkdir()
    copied = []
    monkeypatch.setattr(service, "_private_copy", lambda *args: copied.append(args))
    return root, destination, private, copied


def synthetic_file(directory, name):
    path = directory / name
    content = f"synthetic {name}".encode()
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def test_visual_backup_preserves_unique_assets_after_duplicate_and_each_type_bound(backup):
    root, destination, private, copied = backup
    canonical, canonical_hash = synthetic_file(private, "canonical.png")
    svg, svg_hash = synthetic_file(private, "native.svg")
    source, source_hash = synthetic_file(private, "native.json")
    html, html_hash = synthetic_file(private, "interface.html")
    rows = [{"asset_path": str(canonical), "canonical_hash": canonical_hash,
             "provenance": {"svg_path": str(svg), "svg_hash": svg_hash, "source_path": str(source), "source_hash": source_hash}},
            {"asset_path": str(canonical), "canonical_hash": canonical_hash,
             "provenance": {"source_path": str(html), "source_hash": html_hash}}]
    result = service.backup_visual_assets({"design_versions": rows}, root, destination)
    expected = [(canonical, canonical_hash, 64 * 1024 * 1024), (svg, svg_hash, 1024 * 1024),
                (source, source_hash, 1024 * 1024), (html, html_hash, 1024 * 1024)]
    assert len(result) == len(copied) == len(expected)
    for entry, call, (path, digest, maximum) in zip(result, copied, expected, strict=True):
        key = f"visual_assets/{digest}{path.suffix}"
        assert entry == {"key": key, "sha256": digest, "original_path": str(path),
                         "relative_path": path.relative_to(root).as_posix(), "maximum_bytes": maximum}
        assert call == (path, destination / key, digest, maximum)


def test_visual_backup_accepts_empty_history_and_legacy_without_native_sources(backup):
    root, destination, private, copied = backup
    assert service.backup_visual_assets({}, root, destination) == []
    source, digest = synthetic_file(private, "canonical.png")
    rows = [{"asset_path": str(source), "canonical_hash": digest}]
    items = service.backup_visual_assets({"design_versions": rows}, root, destination)
    assert len(items) == len(copied) == 1 and items[0]["sha256"] == digest


@pytest.mark.parametrize("invalid", ["foreign", "symlink", "linked_parent", "wrong_extension", "missing_hash", "linked_root"])
def test_visual_backup_rejects_unowned_or_untyped_sources_before_copy(backup, tmp_path, invalid):
    root, destination, private, copied = backup
    source, digest = synthetic_file(private, "canonical.png")
    if invalid == "foreign":
        source, digest = synthetic_file(tmp_path, "foreign.png")
    elif invalid == "symlink":
        link = private / "linked.png"
        link.symlink_to(source)
        source = link
    elif invalid == "linked_parent":
        link = root / "visual" / "linked"
        link.symlink_to(private, target_is_directory=True)
        source = link / "canonical.png"
    elif invalid == "wrong_extension":
        source, digest = synthetic_file(private, "unknown.bmp")
    elif invalid == "missing_hash":
        digest = None
    else:
        link = tmp_path / "linked_root"
        link.symlink_to(root, target_is_directory=True)
        root = link
    with pytest.raises(VisualLimit):
        service.backup_visual_assets({"design_versions": [{"asset_path": str(source), "canonical_hash": digest}]}, root, destination)
    assert copied == []
    assert Path(source).exists()
