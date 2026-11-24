"""Synthetic-pixel visual contracts; no real image/vision requests or quality claims."""
from dataclasses import asdict, replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Meeting
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.fixtures import sample_brief
from round_robin.providers.images import ClosedImageFundingGateway, ImageAuthorization, ImageFundingLocked
from round_robin.visual.assets import UnsafeRaster, store_raster, validate_raster, verified_bytes
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.jobs import ImageJob, recover_saved_job, request_hash, submit_saved_job
from round_robin.visual.renderers.diagram import Diagram, render_diagram
from round_robin.visual.renderers.interface import IsolatedRendererRequired, NativeRendererConfiguration, render_interface
from round_robin.visual.reviews import PixelView, coverage, independent_review_packet, issue_review_receipt, validate_critique
from round_robin.visual.schemas import Critique, Finding, ImageCapabilities, Region, VisualBrief
from round_robin.visual.service import backup_visual_assets, commit_review, commit_version, purge_designs, quarantine_restored_designs, reap_asset_reservations, reserve_asset_storage, restore_visual_assets
from round_robin.worker.image_tasks import IMAGE_HEARTBEAT_SECONDS, IMAGE_LEASE_SECONDS, image_app, image_request_timeout


def png(size=(48, 32), *, metadata=False):
    output = BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("Location", "private-location-sentinel")
    Image.new("RGB", size, "#336644").save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def caps(provider="anthropic", **changes):
    return ImageCapabilities(provider=provider, endpoint_version="synthetic-fixture-v1", understands_images=True,
                             supported_media=["image/png"], **changes)


def receipt(provider="anthropic", version="version_one"):
    raster = validate_raster(png())
    views = [PixelView(raster.canonical_bytes, raster.canonical_hash)]
    return issue_review_receipt(caps(provider), version, raster.canonical_hash, views), views


def critique(delivery):
    return Critique(provider=delivery.provider, version_id=delivery.version_id, canonical_hash=delivery.canonical_hash,
                    receipt_id=delivery.id, findings=[Finding(id="finding_one", description="Synthetic test observation",
                    category="observed_defect", severity="minor", region=Region(x=.1, y=.2, width=.2, height=.2))])


def test_raster_normalization_strips_metadata_preserves_canonical_pixels_and_private_bytes(tmp_path):
    original = png(metadata=True)
    raster = validate_raster(original, "image/png")
    assert raster.original_hash == hashlib.sha256(original).hexdigest()
    assert b"private-location-sentinel" not in raster.canonical_bytes
    with Image.open(BytesIO(raster.canonical_bytes)) as image:
        assert image.size == (48, 32) and not image.info
    target = store_raster(tmp_path / "private", raster)
    assert target.stat().st_mode & 0o077 == 0
    assert store_raster(tmp_path / "private", raster) == target
    target.write_bytes(b"corrupt")
    with pytest.raises(UnsafeRaster, match="checksum"):
        verified_bytes(target, raster.canonical_hash)


def test_jpeg_orientation_is_applied_before_metadata_is_discarded():
    image = Image.new("RGB", (3, 5), "red")
    exif = image.getexif()
    exif[274] = 6
    exif[270] = "private-description-sentinel"
    output = BytesIO()
    image.save(output, format="JPEG", exif=exif)
    normalized = validate_raster(output.getvalue(), "image/jpeg")
    assert (normalized.width, normalized.height) == (5, 3)
    assert b"private-description-sentinel" not in normalized.canonical_bytes


@pytest.mark.parametrize("data,media", [(b"", "image/png"), (b"<svg onload='x'/>", "image/png"), (b"GIF89a", "image/png"), (b"not pixels", None)])
def test_invalid_raster_bytes_never_become_approved_pixels(data, media):
    with pytest.raises(UnsafeRaster):
        validate_raster(data, media)


def test_image_extension_mime_size_pixels_and_animation_are_not_trusted():
    with pytest.raises(UnsafeRaster, match="Declared"):
        validate_raster(png(), "image/jpeg")
    with pytest.raises(UnsafeRaster, match="ten MiB"):
        validate_raster(b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(UnsafeRaster, match="megapixels"):
        validate_raster(png((4001, 4000)))
    frames = BytesIO()
    Image.new("RGB", (4, 4)).save(frames, format="PNG", save_all=True, append_images=[Image.new("RGB", (4, 4), "red")], duration=50, loop=0)
    with pytest.raises(UnsafeRaster):
        validate_raster(frames.getvalue())


def test_pixel_receipts_bind_actual_bytes_hash_version_capability_and_initial_independence():
    delivery, views = receipt()
    brief = VisualBrief(purpose="Synthetic test", audience="Test reviewer")
    packet = independent_review_packet(brief, delivery, views)
    assert packet["images"][0] == views[0].image_bytes and "peers" not in packet
    assert validate_critique(critique(delivery), delivery)
    with pytest.raises(ValueError, match="actual"):
        validate_critique(critique(delivery).model_copy(update={"version_id": "version_other"}), delivery)
    with pytest.raises(ValueError, match="cannot inspect"):
        issue_review_receipt(caps().model_copy(update={"understands_images": False}), "version_one", delivery.canonical_hash, views)
    with pytest.raises(ValueError, match="outgoing"):
        issue_review_receipt(caps(), "version_one", delivery.canonical_hash, [PixelView(b"not pixels", delivery.canonical_hash)])
    with pytest.raises(ValueError, match="three"):
        issue_review_receipt(caps(), "version_one", delivery.canonical_hash, views * 5)
    with pytest.raises(ValueError, match="Outgoing"):
        independent_review_packet(brief, delivery, [])
    with pytest.raises(ValueError, match="changed"):
        independent_review_packet(brief, delivery, [PixelView(png((12, 12)), views[0].sha256)])


def test_two_distinct_outside_critics_are_required_and_unknown_origin_stays_disclosed():
    deliveries = [receipt(provider)[0] for provider in ("openai", "anthropic", "google")]
    critiques = [critique(item) for item in deliveries]
    basis = {"version_id": "version_one", "canonical_hash": deliveries[0].canonical_hash}
    assert coverage(critiques[:2], deliveries[:2], generator_provider="openai", **basis)["complete"] is False
    assert coverage(critiques, deliveries, generator_provider="openai", **basis)["complete"] is True
    unknown = coverage(critiques[:2], deliveries[:2], generator_provider=None, **basis)
    assert unknown["complete"] is True and unknown["generator_provenance"] == "unknown" and unknown["limitations"]
    with pytest.raises(ValidationError):
        caps("openai-model-alias")


@pytest.mark.parametrize("region", [{"x": .9, "y": 0, "width": .2, "height": .1}, {"x": float("nan"), "y": 0, "width": .1, "height": .1}])
def test_invented_out_of_bounds_or_nonfinite_regions_are_rejected(region):
    with pytest.raises(ValidationError):
        Region(**region)


def authorize(state, identity, *, kind="vision", step=None, now=1000, bound="0", **kwargs):
    state.authorize(identity, step or identity, kind, bound, now=now, expected_fence=state.fence, epoch=state.epoch, **kwargs)


def test_four_submissions_two_calls_per_step_and_shared_24_calls_survive_unknowns_and_restart():
    state = VisualState("improve", "10", 1000)
    for index in range(4):
        authorize(state, f"image_{index}", kind="generation", step=f"image_step_{index // 2}", bound="1")
        state.settle(f"image_{index}", None)
    restored = VisualState(**asdict(state))
    assert restored.submission_count == 4 and restored.exposure() == 4
    with pytest.raises(VisualLimit, match="submission"):
        authorize(restored, "image_5", kind="generation", step="fresh_step", bound="1")
    calls = VisualState("improve", "0", 1000)
    for index in range(22):
        authorize(calls, f"vision_{index}")
    with pytest.raises(VisualLimit, match="protected"):
        authorize(calls, "optional_23")
    authorize(calls, "final_critic_1", review_calls=1)
    authorize(calls, "final_critic_2", review_calls=0)
    with pytest.raises(VisualLimit):
        authorize(calls, "new_identity", review_calls=0)
    corrupted = asdict(calls)
    corrupted["request_count"] = 0
    with pytest.raises(VisualLimit, match="journal"):
        VisualState(**corrupted)


def test_protected_money_time_unknown_exposure_and_higher_actual_usage_never_become_free():
    state = VisualState("improve", "1", 1000)
    with pytest.raises(VisualLimit, match="headroom"):
        authorize(state, "too_much", bound="0.7", review_money="0.4")
    authorize(state, "one", bound="0.6", review_money="0.4")
    state.settle("one", None)
    assert state.exposure() == __import__("decimal").Decimal("0.6")
    state.settle("one", "1.20")
    assert state.exposure() == __import__("decimal").Decimal("1.20")
    with pytest.raises(VisualLimit):
        authorize(state, "later", bound="0")
    with pytest.raises(VisualLimit, match="time"):
        authorize(VisualState("improve", "1", 1000), "late", now=2700)
    with pytest.raises(VisualLimit):
        authorize(VisualState("improve", "1", 1000), "bad_clock", now=float("nan"))
    with pytest.raises(VisualLimit, match="Unknown"):
        authorize(VisualState("improve", "1", 1000), "no_price", bound=None)


def test_versions_stop_at_v3_review_only_cannot_edit_and_cancel_fences_future_work():
    state = VisualState("improve", "0", 1000)
    assert state.publish_version(parent_hash=None, expected_parent_hash=None) == 1
    assert state.publish_version(parent_hash="hash_one", expected_parent_hash="hash_one") == 2
    assert state.publish_version(parent_hash="hash_two", expected_parent_hash="hash_two") == 3
    with pytest.raises(VisualLimit):
        state.publish_version(parent_hash="hash_three", expected_parent_hash="hash_three")
    read_only = VisualState("review_only", "0", 1000)
    read_only.publish_version(parent_hash=None, expected_parent_hash=None)
    with pytest.raises(VisualLimit):
        read_only.publish_version(parent_hash="one", expected_parent_hash="one")
    read_only.cancel()
    with pytest.raises(VisualLimit, match="fenced"):
        authorize(read_only, "late_response")


class FakeImageProvider:
    def __init__(self, *, fail=False, lookup=True):
        self.capabilities = caps("openai", generates_images=True, asynchronous_jobs=True, job_lookup=lookup, idempotent_submission=True)
        self.fail = fail
        self.submissions = 0

    def submit(self, packet, authorization, *, idempotency_key):
        assert idempotency_key == "image_job_one"
        self.submissions += 1
        if self.fail:
            raise TimeoutError("accepted remotely; acknowledgement lost")
        return {"job_id": "remote_job_one"}

    def lookup(self, job_id, authorization):
        assert job_id == "remote_job_one"
        return {"status": "completed"}


def test_job_persists_uncertain_send_before_submit_and_never_blindly_resubmits():
    provider = FakeImageProvider(fail=True)
    packet = {"brief": "Synthetic fixture only"}
    job = ImageJob("image_job_one", "operation_one", "step_one", "openai", request_hash(packet), 0, 1, "owner_one", "session_one", "0")
    auth = ImageAuthorization("operation_one", "owner_one", "session_one", "0", 0, 1, step_id="step_one")
    records = []
    with pytest.raises(TimeoutError):
        submit_saved_job(job, packet, provider, auth, persist=lambda value: records.append(asdict(value)))
    assert records[0]["status"] == "submitting" and records[0]["unknown_outcome"]
    assert records[-1]["status"] == "unknown"
    with pytest.raises(VisualLimit):
        submit_saved_job(job, packet, provider, auth, persist=lambda value: None)
    assert provider.submissions == 1
    with pytest.raises(VisualLimit, match="lookup"):
        recover_saved_job(job, VisualState("improve", "0", 1000), provider, auth, now=1001, persist=lambda value: None)


def test_supported_same_job_lookup_counts_polls_without_another_generation():
    provider = FakeImageProvider()
    packet = {"brief": "Synthetic fixture only"}
    job = ImageJob("image_job_one", "operation_one", "step_one", "openai", request_hash(packet), 0, 1, "owner_one", "session_one", "0")
    auth = ImageAuthorization("operation_one", "owner_one", "session_one", "0", 0, 1, step_id="step_one")
    submit_saved_job(job, packet, provider, auth, persist=lambda value: None)
    state = VisualState("improve", "0", 1000)
    with pytest.raises(VisualLimit, match="authority"):
        recover_saved_job(job, state, provider, replace(auth, owner_id="owner_foreign"), now=1001, persist=lambda value: None)
    for _ in range(60):
        recover_saved_job(job, state, provider, auth, now=1001, persist=lambda value: None)
    with pytest.raises(VisualLimit, match="sixty"):
        recover_saved_job(job, state, provider, auth, now=1001, persist=lambda value: None)
    assert provider.submissions == 1
    state.cancel()
    with pytest.raises(VisualLimit, match="authority"):
        recover_saved_job(job, state, provider, auth, now=1001, persist=lambda value: None)


def test_image_worker_is_isolated_and_live_gateway_remains_closed():
    assert image_app.conf.task_time_limit == 660 and image_app.conf.task_soft_time_limit == 630
    assert image_app.conf.broker_transport_options["visibility_timeout"] == 900
    assert (IMAGE_LEASE_SECONDS, IMAGE_HEARTBEAT_SECONDS) == (720, 10)
    assert image_request_timeout(1800, 0) == 600
    with pytest.raises(ValueError):
        image_request_timeout(20, 0)
    with pytest.raises(ImageFundingLocked):
        ClosedImageFundingGateway().authorize(operation_id="not_funded")


def test_native_diagram_is_editable_reproducible_escaped_and_structurally_checked():
    source = {"width": 600, "height": 300, "nodes": [{"id": "node_one", "label": "<script>", "x": 20, "y": 20}, {"id": "node_two", "label": "Result", "x": 300, "y": 120}], "edges": [{"source": "node_one", "target": "node_two", "label": "input"}]}
    rendered = render_diagram(Diagram.model_validate(source))
    assert b"&lt;script&gt;" in rendered.svg and b"<script>" not in rendered.svg
    assert b"href=" not in rendered.svg and b"onload=" not in rendered.svg
    assert rendered.source_hash == render_diagram(Diagram.model_validate(source)).source_hash
    assert rendered.raster.width == 600
    with pytest.raises(ValidationError):
        Diagram.model_validate({**source, "edges": [{"source": "node_one", "target": "foreign_node"}]})
    with pytest.raises(ValidationError):
        Diagram.model_validate({**source, "nodes": [{"id": "node_one", "label": "Outside", "x": 590, "y": 20}]})
    with pytest.raises(IsolatedRendererRequired):
        render_interface("<script>read host filesystem</script>", configuration=NativeRendererConfiguration(enabled=False, image=""))


@pytest.fixture
def visual_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_VISUAL_ENABLED", "1")
    app = create_app(f"sqlite:///{tmp_path / 'visual_test.sqlite'}", settings=replace(Settings(), artifact_directory=tmp_path / "artifacts"), initialize=True)
    with TestClient(app) as client:
        created = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")}).json()
        body = {"command_id": str(uuid4()), "mode": "review_only", "brief": {"purpose": "Synthetic visual comparison", "audience": "Test reviewer"}, "recipients": ["anthropic", "google"], "maximum_usd": "0"}
        response = client.post(f"/api/meetings/{created['id']}/design-sessions", json=body)
        assert response.status_code == 201, response.text
        yield app, client, created, response.json()


def upload_original(client, design):
    response = client.post(f"/api/design-sessions/{design['id']}/images", content=png(metadata=True), headers={"Content-Type": "image/png", "X-Command-Id": str(uuid4()), "X-Expected-Revision": str(design["revision"]), "X-Content-Sha256": hashlib.sha256(png(metadata=True)).hexdigest()})
    assert response.status_code == 201, response.text
    return response.json()


def test_optional_api_upload_selection_decline_preflight_and_owner_fences(visual_app):
    app, client, meeting, design = visual_app
    saved = upload_original(client, design)
    assert len(saved["versions"]) == 1
    version = saved["versions"][0]
    assert version["coverage"]["complete"] is False and version["coverage"]["generator_provenance"] == "unknown"
    image = client.get(version["image_url"])
    assert image.status_code == 200 and b"private-location-sentinel" not in image.content
    selected = client.post(f"/api/design-sessions/{design['id']}/selection", json={"command_id": str(uuid4()), "expected_revision": saved["revision"], "version_id": version["id"]}).json()
    assert selected["selected_version_id"] == version["id"]
    declined = client.post(f"/api/design-sessions/{design['id']}/selection", json={"command_id": str(uuid4()), "expected_revision": selected["revision"], "version_id": None}).json()
    assert declined["selection_recorded"] and declined["selected_version_id"] is None
    pre = client.post(f"/api/design-sessions/{design['id']}/preflight", json={"command_id": str(uuid4()), "expected_revision": declined["revision"]}).json()
    assert pre["blockers"] and pre["input_hashes"] == [version["canonical_hash"]]
    assert client.post(f"/api/design-sessions/{design['id']}/reviews", json={"command_id": str(uuid4()), "expected_revision": declined["revision"], "preflight_id": pre["id"]}).status_code == 409
    app.state.identity_provider = lambda request: "owner_foreign"
    assert client.get(version["image_url"]).status_code == 404
    app.state.identity_provider = lambda request: "owner_local"
    with app.state.session_factory.begin() as session:
        session.get(Meeting, meeting["id"]).deleted_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    assert client.get(version["image_url"]).status_code == 410


def test_default_visual_flag_is_closed_even_when_caller_has_provider_credentials(visual_app, monkeypatch):
    _, client, _, design = visual_app
    monkeypatch.delenv("ROUND_ROBIN_VISUAL_ENABLED")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-sentinel-not-a-real-key")
    assert client.get("/api/visual/capabilities").json()["enabled"] is False
    assert client.get(f"/api/design-sessions/{design['id']}").status_code == 404


def test_deletion_and_restore_preserve_unknowns_fence_late_publication_and_purge_owned_pixels(visual_app, tmp_path):
    app, client, meeting, design = visual_app
    saved = upload_original(client, design)
    with app.state.session_factory.begin() as session:
        row = session.get(DesignSession, design["id"])
        state = VisualState(**row.state)
        state.paid_admitted = True
        authorize(state, "unknown_review", bound="0", now=state.admitted_at)
        row.state = asdict(state)
        quarantine_restored_designs(session, 9)
    with app.state.session_factory.begin() as session:
        row = session.get(DesignSession, design["id"])
        assert row.status == "quarantined" and row.state["operations"]["unknown_review"]["status"] == "unknown"
        with pytest.raises(VisualLimit):
            commit_version(session, row, validate_raster(png()), app.state.settings.artifact_directory / "visual", expected_fence=0)
        purge_designs(session, meeting["id"], app.state.settings.artifact_directory)
    with app.state.session_factory() as session:
        row = session.get(DesignSession, design["id"])
        assert row.state["operations"]["unknown_review"]["status"] == "unknown"
        assert row.frozen_brief == {} and list(session.scalars(select(DesignVersion))) == []
    assert list((app.state.settings.artifact_directory / "visual").rglob("*.png")) == []


def test_visual_backup_restore_rebases_private_assets_and_rejects_tampering(visual_app, tmp_path):
    app, client, _, design = visual_app
    upload_original(client, design)
    with app.state.session_factory() as session:
        row = session.scalar(select(DesignVersion))
        tables = {"design_versions": [{"asset_path": row.asset_path, "canonical_hash": row.canonical_hash, "provenance": row.provenance}]}
    backup_root = tmp_path / "backup"
    items = backup_visual_assets(tables, app.state.settings.artifact_directory, backup_root)
    restore_visual_assets(backup_root, tmp_path / "restored", items, tables)
    restored = Path(tables["design_versions"][0]["asset_path"])
    assert restored.is_file() and restored.is_relative_to(tmp_path / "restored" / "visual")
    (backup_root / items[0]["key"]).write_bytes(b"corrupt")
    with pytest.raises(VisualLimit, match="checksum"):
        restore_visual_assets(backup_root, tmp_path / "second_restore", items, {"design_versions": []})


def test_pending_visual_byte_holds_enforce_owner_quota_and_stale_upload_recovery(visual_app):
    from datetime import timedelta
    from round_robin.database.models import utcnow
    app, _, _, design = visual_app
    with app.state.session_factory.begin() as session:
        row = session.get(DesignSession, design["id"])
        for index in range(4):
            hold = reserve_asset_storage(session, row, f"visual_hold_{index}")
            hold.created_at = utcnow() - timedelta(minutes=11)
            session.flush()
        with pytest.raises(VisualLimit, match="storage"):
            reserve_asset_storage(session, row, "visual_hold_over_quota")
    with app.state.session_factory.begin() as session:
        assert reap_asset_reservations(session, app.state.settings.artifact_directory) == 4
    with app.state.session_factory.begin() as session:
        row = session.get(DesignSession, design["id"])
        reserve_asset_storage(session, row, "visual_hold_new")
        session.flush()
        assert session.get(DesignOperation, "visual_hold_new").payload["status"] == "reserved"


def test_explicit_native_diagram_api_preserves_three_versions_and_delivers_exact_editable_source(visual_app, monkeypatch):
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    created = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={"command_id": str(uuid4()), "mode": "improve", "brief": {"purpose": "Inspect a bounded flow", "audience": "Test reviewer", "domain": "structured_diagram", "width": 500, "height": 200, "required_text": ["Input"]}, "recipients": [], "maximum_usd": "0"}).json()
    source = {"width": 500, "height": 200, "nodes": [{"id": "node_one", "label": "Input", "x": 20, "y": 20}], "edges": []}
    original = None
    for index in range(3):
        source["nodes"][0]["x"] = 20 + index * 10
        response = client.post(f"/api/design-sessions/{created['id']}/native-diagram", json={"command_id": str(uuid4()), "expected_revision": created["revision"], "diagram": source})
        assert response.status_code == 201, response.text
        created = response.json()
        original = original or created["versions"][0]
    assert len(created["versions"]) == 3 and created["versions"][0] == original
    current = created["versions"][-1]
    download = client.get(f"/api/design-versions/{current['id']}/source.svg")
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    assert hashlib.sha256(download.content).hexdigest() == current["provenance"]["svg_hash"]
    assert current["coverage"]["complete"] is False and current["source_kind"] == "native_diagram"
    assert client.post(f"/api/design-sessions/{created['id']}/native-diagram", json={"command_id": str(uuid4()), "expected_revision": created["revision"], "diagram": source}).status_code == 409


def test_native_interface_api_stores_checks_and_downloads_source_without_executing_it_on_app_origin(visual_app, monkeypatch):
    from types import SimpleNamespace
    from round_robin.api import visual as routes
    _, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED", "1")
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_RENDERER_IMAGE", "synthetic-fixture-image")
    source = "<button id='run'>Run</button><script>window.privateTest=true</script>"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    returned = SimpleNamespace(source=source, source_hash=source_hash, raster=validate_raster(png()), renderer_version="synthetic-native-fixture", configuration_hash="a" * 64, checks=[{"status": "passed"}], snapshot=SimpleNamespace(model_dump=lambda **kwargs: {"source_sha256": source_hash, "fixture": True}))
    monkeypatch.setattr(routes, "render_interface", lambda *args, **kwargs: returned)
    created = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={"command_id": str(uuid4()), "mode": "review_only", "brief": {"purpose": "Inspect a button", "audience": "Test reviewer", "domain": "native_interface"}, "maximum_usd": "0"}).json()
    response = client.post(f"/api/design-sessions/{created['id']}/native-interface", json={"command_id": str(uuid4()), "expected_revision": created["revision"], "source": source, "actions": []})
    assert response.status_code == 201, response.text
    version = response.json()["versions"][0]
    assert version["provenance"]["checks"] == [{"status": "passed"}]
    download = client.get(f"/api/design-versions/{version['id']}/source.html")
    assert download.text == source and download.headers["content-type"].startswith("text/plain")
    assert "attachment" in download.headers["content-disposition"] and "sandbox" in download.headers["content-security-policy"]
