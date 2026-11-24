"""Regressions for the confirmed image/native findings from the deep audits."""
from datetime import timedelta
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from pathlib import Path
import random
from types import SimpleNamespace
from uuid import uuid4

import httpx
from PIL import Image
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from backend.tests.test_image_provider import authorization, image_configuration, pixels
from backend.tests.test_image_worker import FakeImageAdapter, image_factory, seed_image
from backend.tests.test_exhaustive_visual_provider import vision_packet
from backend.tests.test_visual import png, upload_original, visual_app
from backend.tests.test_visual_review_regressions import create, prepare
from round_robin.api import visual as routes
from round_robin.database.models import BudgetOperation, Command, utcnow
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import CriticContent, HTTPImageProvider
from round_robin.visual.assets import MAX_UPLOAD_BYTES, UnsafeRaster, validate_canonical_raster, validate_raster
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.renderers.diagram import Diagram, render_diagram
from round_robin.visual.reviews import PixelView, independent_review_packet, issue_review_receipt
from round_robin.visual.schemas import ImageCapabilities, VisualBrief
from round_robin.worker import image_tasks as worker


def test_anthropic_critic_wire_subset_preserves_full_local_validation(image_configuration):
    raw_schema = CriticContent.model_json_schema(mode="serialization")
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        schema = body["output_config"]["format"]["schema"]
        def check(value):
            if isinstance(value, dict):
                assert not {"minLength", "maxLength", "minimum", "maximum", "maxItems"} & value.keys()
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(schema)
        assert "maxLength=500" in schema["$defs"]["Finding"]["properties"]["description"]["description"]
        invalid = {"findings": [{"id": "finding_one", "description": "x" * 501, "category": "observed_defect", "severity": "minor"}], "limitations": []}
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(invalid)}]})
    adapter = HTTPImageProvider("anthropic", image_configuration, transport=httpx.MockTransport(respond), api_key="synthetic")
    with pytest.raises(ProviderFailure, match="schema_invalid"):
        adapter.review(vision_packet(adapter), authorization("vision"))
    assert len(requests) == 1
    assert raw_schema == CriticContent.model_json_schema(mode="serialization")


def test_generation_and_each_critic_leave_the_other_required_work_time(image_factory, image_configuration, tmp_path, monkeypatch):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    clock = [utcnow()]
    monkeypatch.setattr(worker, "utcnow", lambda: clock[0])
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        design.state = {**design.state, "paid_admitted": True, "admitted_at": clock[0].timestamp() - 1500}
    def spend_allowance(packet, authorization):
        clock[0] += timedelta(seconds=packet["timeout_seconds"])
    registry = {provider: FakeImageAdapter(provider, image_configuration, image_factory, callback=spend_allowance) for provider in ("openai", "anthropic", "google")}
    assert worker.execute_image_operation(operation_id, session_factory=image_factory, provider_registry=registry, artifact_directory=tmp_path)["status"] == "persisted"
    assert 0 < registry["openai"].packets[0]["timeout_seconds"] <= 85
    with image_factory() as session:
        reviews = [row.id for row in session.scalars(select(DesignOperation)) if row.payload.get("kind") == "vision"]
    for identity in reviews:
        assert worker.execute_image_operation(identity, session_factory=image_factory, provider_registry=registry, artifact_directory=tmp_path)["status"] == "persisted"
    assert [registry[provider].calls for provider in ("openai", "anthropic", "google")] == [1, 1, 1]
    with image_factory() as session:
        design = session.get(DesignSession, design_id)
        assert clock[0].timestamp() - design.state["admitted_at"] <= 1775
        assert Decimal(session.get(BudgetOperation, f"visualfinish_{design_id}").reserved_usd) == 0
    state = VisualState("improve", "2", 0)
    with pytest.raises(VisualLimit, match="Request timeout"):
        state.authorize("operation", "step", "generation", ".4", now=1500, expected_fence=0, epoch=1, request_seconds=86)


@pytest.mark.parametrize("dimensions", [(9000, 1), (1, 9000)])
def test_out_of_axis_upload_is_rejected_without_breaking_history(visual_app, dimensions):
    app, client, meeting, design = visual_app
    content = png(dimensions)
    response = client.post(f"/api/design-sessions/{design['id']}/images", content=content, headers={"Content-Type": "image/png", "X-Command-Id": str(uuid4()), "X-Expected-Revision": str(design["revision"]), "X-Content-SHA256": hashlib.sha256(content).hexdigest()})
    assert response.status_code == 422
    assert client.get(f"/api/design-sessions/{design['id']}").status_code == 200
    assert client.get(f"/api/meetings/{meeting['id']}/design-sessions").status_code == 200
    with app.state.session_factory() as session:
        assert session.scalar(select(DesignVersion)) is None


def test_legacy_wide_image_remains_readable_with_an_actionable_preflight_blocker(visual_app):
    app, client, meeting, design = visual_app
    saved = upload_original(client, design)
    content = png((9000, 1))
    # Model a genuine old saved artifact, without passing today's upload guard.
    with app.state.session_factory.begin() as session:
        version = session.get(DesignVersion, saved["versions"][0]["id"])
        digest = hashlib.sha256(content).hexdigest()
        path = Path(version.asset_path).with_name(digest + ".png")
        path.write_bytes(content)
        version.asset_path, version.canonical_hash, version.original_hash = str(path), digest, digest
        version.width, version.height = 9000, 1
        version.provenance = {**version.provenance, "canonical_bytes": len(content)}
    response = client.get(f"/api/design-sessions/{design['id']}")
    assert response.status_code == 200
    history = response.json()
    assert history["versions"][0]["width"] == 9000
    assert any("8192" in value for value in history["versions"][0]["coverage"]["limitations"])
    assert client.get(history["versions"][0]["image_url"]).content == content
    assert client.get(f"/api/meetings/{meeting['id']}/design-sessions").status_code == 200
    assert any("historical image" in value for value in prepare(client, history, "review")["blockers"])


def test_large_normalized_jpeg_can_be_reviewed_under_its_canonical_allowance():
    image = Image.frombytes("RGB", (2200, 2200), random.Random(7).randbytes(2200 * 2200 * 3))
    encoded = BytesIO()
    image.save(encoded, format="JPEG", quality=95)
    assert len(encoded.getvalue()) < MAX_UPLOAD_BYTES
    raster = validate_raster(encoded.getvalue(), "image/jpeg")
    assert len(raster.canonical_bytes) > MAX_UPLOAD_BYTES
    views = [PixelView(raster.canonical_bytes, raster.canonical_hash)]
    small = ImageCapabilities(provider="anthropic", endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
    with pytest.raises(ValueError, match="bounded outgoing"):
        issue_review_receipt(small, "version_large", raster.canonical_hash, views)
    large = small.model_copy(update={"maximum_input_bytes": 64 * 1024 * 1024})
    receipt = issue_review_receipt(large, "version_large", raster.canonical_hash, views)
    packet = independent_review_packet(VisualBrief(purpose="Canonical JPEG receipt", audience="Test"), receipt, views)
    assert packet["images"] == [raster.canonical_bytes]
    with pytest.raises(UnsafeRaster, match="ten MiB"):
        validate_raster(raster.canonical_bytes, "image/png")
    assert validate_canonical_raster(raster.canonical_bytes).canonical_hash == raster.canonical_hash


def test_error_response_stream_cannot_renew_the_absolute_image_allowance(image_configuration, monkeypatch):
    clock = [0.0]
    class SlowError(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b'{"error":', b'{"code":', b'"safety"}}'):
                clock[0] += .06
                yield chunk
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(lambda request: httpx.Response(400, stream=SlowError())), api_key="synthetic")
    monkeypatch.setattr("round_robin.providers.images.time.monotonic", lambda: clock[0])
    with pytest.raises(ProviderFailure, match="response_limit"):
        adapter._http("https://unused.invalid", {}, {}, timeout=.1, maximum_bytes=100)
    assert clock[0] == .12


def test_expired_visual_work_releases_only_unused_review_hold(image_factory, image_configuration, tmp_path, monkeypatch):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    adapter = FakeImageAdapter("openai", image_configuration, image_factory, fail=True)
    worker.execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    with image_factory() as session:
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        assert Decimal(session.get(BudgetOperation, f"visualfinish_{design_id}").reserved_usd) == Decimal(".20")
    future = utcnow() + timedelta(seconds=2000)
    monkeypatch.setattr(worker, "utcnow", lambda: future)
    assert worker.reconcile_image_operations(session_factory=image_factory) == 1
    with image_factory() as session:
        design = session.get(DesignSession, design_id)
        assert design.status == design.state["status"] == "expired"
        assert design.execution_fence == design.state["fence"] == 1
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        assert session.get(BudgetOperation, f"visualfinish_{design_id}").status == "released"
    assert worker.reconcile_image_operations(session_factory=image_factory) == 0


@pytest.mark.parametrize("closure", ["configuration_removed", "initial_consent_expired"])
def test_unserviceable_queued_images_become_terminal_without_spending(image_factory, image_configuration, monkeypatch, closure):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    if closure == "configuration_removed":
        monkeypatch.delenv("ROUND_ROBIN_IMAGE_CONFIG_FILE")
    else:
        with image_factory.begin() as session:
            operation = session.get(DesignOperation, operation_id)
            operation.payload = {**operation.payload, "initial_admission_expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
    sends = []
    assert worker.dispatch_pending_image_operations(session_factory=image_factory, sender=sends.append) == 0
    assert sends == []
    with image_factory() as session:
        assert session.get(DesignSession, design_id).status == ("quarantined" if closure == "configuration_removed" else "expired")
        assert session.get(DesignOperation, operation_id).payload["status"] == "blocked"
        assert session.scalar(select(BudgetOperation)) is None


def test_diagram_dimensions_are_bound_before_reserving_renderer_capacity(visual_app, monkeypatch):
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    design = create(client, meeting, maximum="0", domain="structured_diagram")
    identity = str(uuid4())
    source = {"width": 800, "height": 400, "nodes": [{"id": "node_one", "label": "Input", "x": 20, "y": 20}]}
    response = client.post(f"/api/design-sessions/{design['id']}/native-diagram", json={"command_id": identity, "expected_revision": design["revision"], "diagram": source})
    assert response.status_code == 422 and response.json()["error"]["code"] == "native_dimensions_mismatch"
    with app.state.session_factory() as session:
        assert session.get(DesignSession, design["id"]).state["renderer_attempts"] == 0
        assert session.get(Command, ("owner_local", identity)) is None
        assert session.scalar(select(DesignOperation).where(DesignOperation.session_id == design["id"])) is None
    source.update(width=1024, height=1024)
    accepted = client.post(f"/api/design-sessions/{design['id']}/native-diagram", json={"command_id": identity, "expected_revision": design["revision"], "diagram": source})
    assert accepted.status_code == 201 and accepted.json()["render_status"] == "ready"


def test_edge_label_extent_is_checked_before_rendering():
    source = {"width": 1000, "height": 400, "nodes": [
        {"id": "node_a", "label": "A", "x": 750, "y": 50, "width": 60},
        {"id": "node_b", "label": "B", "x": 850, "y": 180, "width": 60}],
        "edges": [{"source": "node_a", "target": "node_b", "label": "This required relationship label is clipped"}]}
    with pytest.raises(ValidationError, match="Relationship label would be clipped"):
        Diagram.model_validate(source)
    source["edges"][0]["label"] = "Fits"
    assert render_diagram(Diagram.model_validate(source)).raster.width == 1000


@pytest.mark.parametrize("label", ["/" * 60, "\\" * 60, "Y" * 60, "S" * 60])
def test_edge_label_preserves_a_client_rasterization_margin(label):
    from round_robin.visual.renderers.diagram import diagram_font
    right = 125 + diagram_font().getbbox(label, anchor="ls")[2]
    source = {"width": right, "height": 200, "nodes": [
        {"id": "node_a", "label": "A", "x": 50, "y": 0, "width": 60, "height": 40},
        {"id": "node_b", "label": "B", "x": 140, "y": 120, "width": 60, "height": 40}],
        "edges": [{"source": "node_a", "target": "node_b", "label": label}]}
    with pytest.raises(ValidationError, match="Relationship label would be clipped"):
        Diagram.model_validate(source)
    source["width"] += 1
    with pytest.raises(ValidationError, match="Relationship label would be clipped"):
        Diagram.model_validate(source)
    source["width"] += 1
    assert render_diagram(Diagram.model_validate(source)).raster.width == right + 2


def test_native_interface_exact_actions_survive_saved_version_reopen(visual_app, monkeypatch):
    _, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED", "1")
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_RENDERER_IMAGE", "synthetic-fixture")
    source = '<button id="submit">Submit</button><p id="result">Saved</p>'
    digest = hashlib.sha256(source.encode()).hexdigest()
    returned = SimpleNamespace(source=source, source_hash=digest, raster=validate_raster(png()), renderer_version="synthetic", configuration_hash="a" * 64, checks=[{"status": "passed"}], snapshot=SimpleNamespace(model_dump=lambda **kwargs: {"source_sha256": digest}))
    monkeypatch.setattr(routes, "render_interface", lambda *args, **kwargs: returned)
    design = create(client, meeting, maximum="0", domain="native_interface")
    actions = [{"action": "click", "selector": "#submit", "assertion": {"selector": "#result", "text": "Saved"}}]
    response = client.post(f"/api/design-sessions/{design['id']}/native-interface", json={"command_id": str(uuid4()), "expected_revision": design["revision"], "source": source, "actions": actions})
    assert response.status_code == 201
    reopened = client.get(f"/api/design-sessions/{design['id']}").json()
    assert reopened["versions"][0]["provenance"]["interaction_plan"] == actions
