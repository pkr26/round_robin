"""Visual audit regressions through real API, pricing and raster boundaries."""
from dataclasses import replace
import base64
from io import BytesIO
import json
from uuid import uuid4

import httpx
from PIL import Image
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from backend.tests.test_image_provider import synthetic_image_configuration
from backend.tests.test_visual import visual_app, upload_original
from round_robin.database.models import BudgetOperation
from round_robin.database.services import lock_policies
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.fixtures import sample_brief
from round_robin.providers import images
from round_robin.visual.renderers.diagram import Diagram, render_diagram
from round_robin.worker.image_tasks import execute_image_operation


def configure(app, monkeypatch, tmp_path, *, maximum="2"):
    configuration = synthetic_image_configuration()
    configuration.pop("supported_sizes")
    configuration["maximum_usd"] = maximum
    path = tmp_path / "review-image-configuration.json"
    path.write_text(json.dumps(configuration))
    monkeypatch.setenv("ROUND_ROBIN_PAID_ACCESS", "1")
    monkeypatch.setenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", str(path))
    app.state.settings = replace(app.state.settings, paid_access_enabled=True)
    with app.state.session_factory.begin() as session:
        for bucket in lock_policies(session, "owner_local"):
            bucket.spending_locked, bucket.limit_usd = False, "10"
            bucket.metadata_json = {"daily_usd": "10", "monthly_usd": "50", "deployment_epoch": 1}
    return images.load_image_configuration()


def create(client, meeting, *, maximum="2", mode="improve", width=1024, height=1024, **brief):
    response = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={
        "command_id": str(uuid4()), "mode": mode, "maximum_usd": maximum,
        "recipients": ["openai", "anthropic", "google"],
        "brief": {"purpose": "Visual review regression", "audience": "Reviewer", "width": width, "height": height, **brief},
    })
    assert response.status_code == 201, response.text
    return response.json()


def prepare(client, design, action):
    response = client.post(f"/api/design-sessions/{design['id']}/preflight", json={
        "command_id": str(uuid4()), "expected_revision": design["revision"], "action": action,
    })
    assert response.status_code == 200, response.text
    return response.json()


def execute(app, identity, provider, adapter):
    return execute_image_operation(identity, session_factory=app.state.session_factory,
                                   provider_registry={provider: adapter}, artifact_directory=app.state.settings.artifact_directory)


def test_generation_capabilities_and_preflight_agree_on_default_sizes(visual_app, monkeypatch, tmp_path):
    app, client, meeting, _ = visual_app
    configuration = configure(app, monkeypatch, tmp_path)
    capabilities = client.get("/api/visual/capabilities").json()
    assert capabilities["supported_sizes"] == ["1024x1024", "1536x1024", "1024x1536"]
    unsupported = create(client, meeting, height=768)
    quote = prepare(client, unsupported, "generate")
    assert any("1024x768" in item and "1024x1024" in item for item in quote["blockers"])
    rejected = client.post(f"/api/design-sessions/{unsupported['id']}/improvements", json={
        "command_id": str(uuid4()), "expected_revision": unsupported["revision"], "preflight_id": quote["id"], "intent": "Use the frozen brief",
    })
    assert rejected.status_code == 409
    with app.state.session_factory() as session:
        assert session.scalar(select(DesignOperation).where(DesignOperation.session_id == unsupported["id"])) is None
        assert session.scalar(select(BudgetOperation).where(BudgetOperation.run_id == unsupported["id"])) is None

    supported = create(client, meeting)
    quote = prepare(client, supported, "generate")
    assert quote["blockers"] == []
    response = client.post(f"/api/design-sessions/{supported['id']}/improvements", json={
        "command_id": str(uuid4()), "expected_revision": supported["revision"], "preflight_id": quote["id"], "intent": "Use the complete frozen brief",
    })
    assert response.status_code == 202, response.text
    outgoing = []
    encoded = BytesIO()
    Image.new("RGB", (1024, 1024), "green").save(encoded, format="PNG")

    def respond(request):
        outgoing.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(encoded.getvalue()).decode()}]})

    adapter = images.HTTPImageProvider("openai", configuration, api_key="synthetic", transport=httpx.MockTransport(respond))
    assert execute(app, response.json()["accepted_operation_ids"][0], "openai", adapter)["status"] == "persisted"
    assert len(outgoing) == 1 and outgoing[0]["size"] == "1024x1024"
    saved = client.get(f"/api/design-sessions/{supported['id']}").json()
    assert (saved["versions"][0]["width"], saved["versions"][0]["height"]) == (1024, 1024)
    assert len(saved["pending_operations"]) == 2


def test_missing_critic_recovery_uses_remaining_allowance_through_http(visual_app, monkeypatch, tmp_path):
    app, client, meeting, _ = visual_app
    configuration = configure(app, monkeypatch, tmp_path, maximum="0.20")
    saved = upload_original(client, create(client, meeting, maximum="0.20", mode="review_only"))
    quote = prepare(client, saved, "review")
    assert quote["blockers"] == []
    started = client.post(f"/api/design-sessions/{saved['id']}/reviews", json={
        "command_id": str(uuid4()), "expected_revision": saved["revision"], "preflight_id": quote["id"],
    })
    assert started.status_code == 202, started.text
    with app.state.session_factory() as session:
        operations = {row.provider: row.id for row in session.scalars(select(DesignOperation).where(DesignOperation.session_id == saved["id"]))}
    text = json.dumps({"findings": [], "limitations": []})
    first = images.HTTPImageProvider("anthropic", configuration, api_key="synthetic", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})))
    missing = images.HTTPImageProvider("google", configuration, api_key="", transport=httpx.MockTransport(lambda request: pytest.fail("Missing credential must block before sending")))
    assert execute(app, operations["anthropic"], "anthropic", first)["status"] == "persisted"
    assert execute(app, operations["google"], "google", missing)["status"] == "blocked"
    current = client.get(f"/api/design-sessions/{saved['id']}").json()
    assert current["state"]["request_count"] == 1
    quote = prepare(client, current, "review")
    assert quote["blockers"] == []  # .10 existing exposure plus one missing .10 critic fits .20.
    resumed = client.post(f"/api/design-sessions/{saved['id']}/reviews", json={
        "command_id": str(uuid4()), "expected_revision": current["revision"], "preflight_id": quote["id"],
    })
    assert resumed.status_code == 202, resumed.text
    assert len(resumed.json()["accepted_operation_ids"]) == 1
    restored = images.HTTPImageProvider("google", configuration, api_key="synthetic-restored", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]})))
    assert execute(app, resumed.json()["accepted_operation_ids"][0], "google", restored)["status"] == "persisted"
    final = client.get(f"/api/design-sessions/{saved['id']}").json()
    assert final["versions"][0]["coverage"]["complete"] is True
    assert final["state"]["request_count"] == 2
    assert final["state"]["maximum_usd"] == "0.20"


@pytest.mark.parametrize("action", ["generate", "edit"])
def test_preflight_dimension_blocker_applies_to_every_generator_action(visual_app, monkeypatch, tmp_path, action):
    app, client, meeting, _ = visual_app
    configure(app, monkeypatch, tmp_path)
    saved = create(client, meeting, height=768)
    if action == "edit":
        saved = upload_original(client, saved)
    quote = prepare(client, saved, action)
    assert any("frozen dimensions 1024x768" in item for item in quote["blockers"])


@pytest.mark.parametrize("label", ["中文", "世界", "🚀"])
def test_unavailable_diagram_glyphs_fail_before_candidate_publication(visual_app, monkeypatch, label):
    _, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    saved = create(client, meeting, maximum="0", mode="review_only", domain="structured_diagram", required_text=[label])
    response = client.post(f"/api/design-sessions/{saved['id']}/native-diagram", json={
        "command_id": str(uuid4()), "expected_revision": saved["revision"],
        "diagram": {"width": 400, "height": 200, "nodes": [{"id": "node_one", "label": label, "x": 20, "y": 20}]},
    })
    assert response.status_code == 422, response.text
    assert "renderer font" in response.text
    current = client.get(f"/api/design-sessions/{saved['id']}").json()
    assert current["versions"] == [] and current["state"]["renderer_attempts"] == 0


def test_supported_diagram_labels_have_distinct_pixels_and_self_edges_are_rejected():
    source = {"width": 400, "height": 200, "nodes": [{"id": "node_one", "label": "Input", "x": 20, "y": 20}]}
    first = render_diagram(Diagram.model_validate(source))
    second = render_diagram(Diagram.model_validate({**source, "nodes": [{**source["nodes"][0], "label": "Output"}]}))
    assert first.raster.canonical_hash != second.raster.canonical_hash
    assert b"Input" in first.svg and b"Output" in second.svg
    with pytest.raises(ValidationError, match="Self relationships"):
        Diagram.model_validate({**source, "edges": [{"source": "node_one", "target": "node_one"}]})
    with pytest.raises(ValidationError, match="renderer font"):
        Diagram.model_validate({**source, "edges": [{"source": "node_one", "target": "node_one", "label": "中文"}]})


def test_diagram_label_fit_uses_rendered_font_metrics():
    with pytest.raises(ValidationError, match="too long"):
        Diagram.model_validate({"width": 400, "height": 200, "nodes": [{"id": "node_one", "label": "W" * 16, "x": 20, "y": 20, "width": 132}]})


def test_deleted_designs_release_slots_without_removing_accounting_tombstones(visual_app):
    app, client, meeting, _ = visual_app
    for index in range(29):
        create(client, meeting, maximum="0", mode="review_only", purpose=f"Quota regression {index}")
    full = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={
        "command_id": str(uuid4()), "mode": "review_only", "brief": {"purpose": "Excess", "audience": "Reviewer"},
    })
    assert full.status_code == 429
    deleted = client.request("DELETE", f"/api/meetings/{meeting['id']}", json={"command_id": str(uuid4())})
    assert deleted.status_code == 200
    with app.state.session_factory() as session:
        tombstones = list(session.scalars(select(DesignSession).where(DesignSession.owner_id == "owner_local")))
        assert len(tombstones) == 30 and all(row.status == "deleted" for row in tombstones)
        assert all(row.frozen_brief == {} for row in tombstones)
    replacement = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")})
    assert replacement.status_code == 201
    created = create(client, replacement.json(), maximum="0", mode="review_only")
    assert created["status"] == "active"
    with app.state.session_factory() as session:
        assert len(list(session.scalars(select(DesignSession)))) == 31


def test_v1_native_source_remains_readable_without_reauthorizing_old_render_rules(visual_app, monkeypatch):
    import hashlib
    from pathlib import Path
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    saved = create(client, meeting, maximum="0", mode="review_only", domain="structured_diagram", width=400, height=200)
    source = {"width": 400, "height": 200, "nodes": [{"id": "node_one", "label": "Input", "x": 20, "y": 20}]}
    response = client.post(f"/api/design-sessions/{saved['id']}/native-diagram", json={
        "command_id": str(uuid4()), "expected_revision": saved["revision"], "diagram": source,
    })
    assert response.status_code == 201, response.text
    version_id = response.json()["versions"][0]["id"]
    # A v1 snapshot legitimately contains source that today's renderer refuses.
    legacy = {**source, "nodes": [{**source["nodes"][0], "label": "中文"}], "edges": [{"source": "node_one", "target": "node_one"}]}
    content = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(content).hexdigest()
    with app.state.session_factory.begin() as session:
        version = session.get(DesignVersion, version_id)
        path = Path(version.asset_path).parent / f"{digest}.json"
        path.write_bytes(content)
        version.provenance = {**version.provenance, "source_path": str(path), "source_hash": digest, "renderer_version": "structured-box-diagram-v1"}
    read = client.get(f"/api/design-versions/{version_id}/source.json")
    assert read.status_code == 200 and read.content == content
    refused = client.post(f"/api/design-sessions/{saved['id']}/native-diagram", json={
        "command_id": str(uuid4()), "expected_revision": response.json()["revision"], "diagram": legacy,
    })
    assert refused.status_code == 422


def test_diagram_font_failure_is_explicit_and_never_publishes_missing_glyphs(monkeypatch):
    from round_robin.visual.renderers import diagram
    diagram.diagram_font.cache_clear()
    diagram.supported_glyph.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(diagram.ImageFont, "load_default", lambda **kwargs: (_ for _ in ()).throw(OSError("font unavailable")))
            with pytest.raises(ValidationError, match="renderer font is unavailable"):
                Diagram.model_validate({"width": 400, "height": 200, "nodes": [{"id": "node_one", "label": "Input", "x": 20, "y": 20}]})
    finally:
        diagram.diagram_font.cache_clear()
        diagram.supported_glyph.cache_clear()
