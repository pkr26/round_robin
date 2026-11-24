"""Private, bounded persistence of rendered interface source."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from round_robin.visual import service
from round_robin.visual.iterations import VisualLimit
from round_robin.visual.renderers.interface import RenderSnapshot


def rendered(source):
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = RenderSnapshot.model_validate({
        "source_sha256": digest, "image_id": "sha256:" + "a" * 64,
        "playwright_version": "test", "browser_version": "test",
        "configuration_hash": "b" * 64, "screenshot_sha256": "c" * 64,
        "canonical_pixel_sha256": "d" * 64,
        "viewport": {"width": 800, "height": 600}, "checks": [],
        "network_attempts": [], "network_attempt_count": 0,
        "page_error_count": 0, "sandbox": {}, "limitations": [],
    })
    return SimpleNamespace(source=source, source_hash=digest, renderer_version="test-renderer",
                           configuration_hash="b" * 64, checks=[], snapshot=snapshot)


@pytest.fixture
def saved_source(tmp_path, monkeypatch):
    design = SimpleNamespace(owner_id="owner_synthetic", id="design_synthetic")
    root = tmp_path / design.owner_id / design.id
    root.mkdir(parents=True)
    version = SimpleNamespace(provenance={"preexisting": "retained"})
    capacity_calls = []
    monkeypatch.setattr(service, "check_storage_capacity", lambda session, owner, size: capacity_calls.append((owner, size)))
    return tmp_path, root, design, version, capacity_calls


def test_source_limit_uses_encoded_bytes_and_accepts_exact_boundary(saved_source):
    directory, root, design, version, calls = saved_source
    exact = rendered("é" * (128 * 1024 // 2))
    service.save_interface_source(None, design, version, exact, directory)
    assert Path(version.provenance["source_path"]).read_bytes() == exact.source.encode()
    assert calls == [(design, 128 * 1024)]
    oversized = rendered("é" * (128 * 1024 // 2) + "x")
    with pytest.raises(VisualLimit):
        service.save_interface_source(None, design, version, oversized, directory)
    assert calls == [(design, 128 * 1024)]
    assert len(list(root.iterdir())) == 1


def test_source_creation_is_private_and_preserves_reusable_provenance(saved_source, monkeypatch):
    directory, root, design, version, calls = saved_source
    value = rendered("<p>synthetic private source</p>")
    real_open = os.open
    creates = []

    def observe(path, flags, mode=0o777, *args, **kwargs):
        if Path(path).parent == root and flags & os.O_CREAT:
            creates.append((flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(service.os, "open", observe)
    service.save_interface_source(None, design, version, value, directory)
    path = root / (value.source_hash + ".html")
    assert path.read_bytes() == value.source.encode()
    assert creates == [(os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)]
    assert path.stat().st_mode & 0o077 == 0
    assert version.provenance == {
        "preexisting": "retained", "source_path": str(path), "source_hash": value.source_hash,
        "native_bytes": len(value.source.encode()), "renderer_version": value.renderer_version,
        "configuration_hash": value.configuration_hash, "checks": value.checks,
        "render_snapshot": value.snapshot.model_dump(mode="json"),
        "functional_scope": "only the declared browser actions/assertions; appearance does not establish full correctness",
    }
    service.save_interface_source(None, design, version, value, directory)
    assert len(creates) == 1
    assert calls == [(design, len(value.source)), (design, len(value.source))]
