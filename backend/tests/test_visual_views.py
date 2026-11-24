from copy import deepcopy
import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.tests.test_visual import visual_app, upload_original
from round_robin.database.models import Command
from round_robin.database.visual_models import DesignVersion
from round_robin.visual.views import DesignPage, DesignSessionView, NativeRenderView, VisualCapabilitiesView, VisualPreflightView, VisualUploadView


def test_visual_json_journey_uses_authoritative_state_and_preflight_views(visual_app):
    app, client, meeting, design = visual_app
    parsed = DesignSessionView.model_validate(design)
    assert parsed.state.request_count == parsed.state.submission_count == 0
    assert parsed.mode == parsed.state.mode
    saved = VisualUploadView.model_validate(upload_original(client, design))
    assert saved.upload_status == "ready"
    assert saved.state.version_count == len(saved.versions) == 1
    assert saved.versions[0].coverage.complete is False
    quote = client.post(f"/api/design-sessions/{design['id']}/preflight", json={"command_id": str(uuid4()), "expected_revision": saved.revision}).json()
    assert VisualPreflightView.model_validate(quote).blockers
    assert DesignPage.model_validate(client.get(f"/api/meetings/{meeting['id']}/design-sessions").json()).items[0].versions[0].id == saved.versions[0].id
    assert VisualCapabilitiesView.model_validate(client.get("/api/visual/capabilities").json()).versions == 3


def test_visual_state_view_rejects_unbacked_counters(visual_app):
    _, _, _, design = visual_app
    invalid = deepcopy(design)
    invalid["state"]["request_count"] = 1
    with pytest.raises(ValidationError, match="counters disagree"):
        DesignSessionView.model_validate(invalid)


def test_native_views_hide_storage_paths_and_reopen_exact_json_source(visual_app, monkeypatch):
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    design = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={"command_id": str(uuid4()), "mode": "review_only", "brief": {"purpose": "Typed source reopening", "audience": "Test reviewer", "domain": "structured_diagram", "width": 500, "height": 200}, "maximum_usd": "0"}).json()
    command_id = str(uuid4())
    body = {"command_id": command_id, "expected_revision": design["revision"], "diagram": {"width": 500, "height": 200, "nodes": [{"id": "node_source", "label": "Saved source", "x": 20, "y": 20}], "edges": []}}
    response = client.post(f"/api/design-sessions/{design['id']}/native-diagram", json=body)
    assert response.status_code == 201, response.text
    saved = NativeRenderView.model_validate(response.json())
    version = saved.versions[0]
    assert saved.render_status == "ready"
    assert "svg_path" not in version.provenance and "source_path" not in version.provenance
    raw = client.get(f"/api/design-versions/{version.id}/source.json")
    assert raw.status_code == 200 and "attachment" in raw.headers["content-disposition"]
    assert raw.json()["nodes"][0]["label"] == "Saved source"
    assert hashlib.sha256(raw.content).hexdigest() == version.provenance["source_hash"]
    with app.state.session_factory.begin() as session:
        stored = session.get(DesignVersion, version.id)
        assert stored.provenance["svg_path"] and stored.provenance["source_path"]
        receipt = session.get(Command, ("owner_local", command_id))
        historical = deepcopy(receipt.result)
        historical["versions"][0]["provenance"] = stored.provenance
        receipt.result = historical
    # Legacy command receipts and same-UUID replays are adapted on read without
    # erasing the immutable server-side native-source record.
    received = client.get(f"/api/commands/{command_id}")
    assert "source_path" not in received.json()["result"]["versions"][0]["provenance"]
    replay = client.post(f"/api/design-sessions/{design['id']}/native-diagram", json=body)
    assert replay.status_code == 201
    assert "svg_path" not in replay.json()["versions"][0]["provenance"]
    with app.state.session_factory() as session:
        assert session.get(Command, ("owner_local", command_id)).result["versions"][0]["provenance"]["source_path"]
