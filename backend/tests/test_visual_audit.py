"""Adversarial visual regressions discovered by the implementation audit."""
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.tests.test_image_provider import image_configuration
from backend.tests.test_image_worker import FakeImageAdapter, image_factory, seed_image
from backend.tests.test_visual import visual_app
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.providers.images import verify_image_configuration
from round_robin.visual.service import design_view
from round_robin.worker.image_tasks import execute_image_operation
from round_robin.database.models import Command
from round_robin.visual.renderers.diagram import Diagram
from round_robin.visual.views import DesignSessionView
from pydantic import ValidationError


@pytest.mark.parametrize("payload", [
    {"status": "completed", "image_bytes": b"invalid-png", "actual_usd": "0.25"},
    {"status": "completed", "actual_usd": "0.25"},
    {"status": "refused", "actual_usd": "0.25"},
    None,
])
def test_invalid_image_response_finishes_once_and_preserves_usage(image_factory, image_configuration, tmp_path, payload):
    design_id, operation_id = seed_image(image_factory, image_configuration)

    class Malformed(FakeImageAdapter):
        def submit(self, packet, authorization, *, idempotency_key):
            self._received(packet, authorization)
            return payload

    adapter = Malformed("openai", image_configuration, image_factory)
    registry = {"openai": adapter}
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry=registry, artifact_directory=tmp_path)
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry=registry, artifact_directory=tmp_path)
    assert adapter.calls == 1
    with image_factory() as session:
        operation = session.get(DesignOperation, operation_id)
        assert operation.payload["status"] == "failed"
        assert operation.payload["lease_owner"] is None
        storage = session.get(DesignOperation, operation.payload["storage_reservation_id"])
        assert storage.payload["status"] == "released"
        assert session.scalar(select(DesignVersion)) is None
        budget = session.get(BudgetOperation, operation_id)
        expected = "0.25" if payload else None
        assert budget.settled_usd == expected
        assert budget.status == ("settled" if expected else "unknown")
        state = session.get(DesignSession, design_id).state
        assert state["operations"][operation_id]["actual"] == expected
        assert state["request_count"] == state["submission_count"] == 1


def test_invalid_critique_does_not_roll_back_reported_usage(image_factory, image_configuration, tmp_path):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": FakeImageAdapter("openai", image_configuration, image_factory)}, artifact_directory=tmp_path)
    with image_factory() as session:
        review_id = session.scalar(select(DesignOperation.id).where(DesignOperation.provider == "anthropic"))

    class WrongReceipt(FakeImageAdapter):
        def review(self, packet, authorization):
            response = super().review(packet, authorization)
            response["actual_usd"] = "0.06"
            response["critique"]["receipt_id"] = "receipt_foreign"
            return response

    adapter = WrongReceipt("anthropic", image_configuration, image_factory)
    execute_image_operation(review_id, session_factory=image_factory, provider_registry={"anthropic": adapter}, artifact_directory=tmp_path)
    with image_factory() as session:
        assert session.scalar(select(DesignReview)) is None
        assert session.get(DesignOperation, review_id).payload["status"] == "failed"
        assert session.get(BudgetOperation, review_id).settled_usd == "0.06"
        assert session.get(DesignSession, design_id).state["operations"][review_id]["actual"] == "0.06"


def test_user_visual_allowance_below_operator_ceiling_preserves_frozen_configuration(visual_app, image_configuration):
    app, client, meeting, _ = visual_app
    app.state.settings = replace(app.state.settings, paid_access_enabled=True)
    created = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={
        "command_id": str(uuid4()), "mode": "improve", "maximum_usd": "1",
        "recipients": ["openai", "anthropic", "google"],
        "brief": {"purpose": "A smaller explicitly approved allowance", "audience": "Test", "width": 64, "height": 64},
    })
    assert created.status_code == 201, created.text
    design = created.json()
    preflight = client.post(f"/api/design-sessions/{design['id']}/preflight", json={
        "command_id": str(uuid4()), "expected_revision": design["revision"], "action": "generate",
    })
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["limits"]["maximum_usd"] == "1"
    assert preflight.json()["blockers"] == []
    with app.state.session_factory() as session:
        saved = session.get(DesignSession, design["id"])
        assert saved.maximum_usd == "1"
        assert saved.configuration == image_configuration
        verify_image_configuration(saved.configuration)


@pytest.mark.parametrize("status", ["blocked", "fenced"])
def test_visual_admission_failures_remain_visible(image_factory, image_configuration, status):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        operation = session.get(DesignOperation, operation_id)
        operation.payload = {**operation.payload, "status": status, "error_code": "synthetic_rejection"}
        result = design_view(session, session.get(DesignSession, design_id))
        assert result["pending_operations"] == []
        assert result["operation_failures"] == [{"id": operation_id, "kind": "generation", "error": "synthetic_rejection"}]


@pytest.mark.parametrize("label", ["Bad\x01label", "Bad\ufffelabel", "Multiple\nlines"])
def test_native_diagram_rejects_invalid_xml_and_multiline_labels(label):
    with pytest.raises(ValidationError):
        Diagram.model_validate({"width": 500, "height": 200,
                                "nodes": [{"id": "node_test", "label": label, "x": 10, "y": 10}], "edges": []})


def test_failed_native_diagram_has_a_terminal_reconcilable_receipt(visual_app, monkeypatch):
    from round_robin.api import visual as routes
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    created = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={
        "command_id": str(uuid4()), "mode": "review_only", "maximum_usd": "0",
        "brief": {"purpose": "Synthetic renderer failure", "audience": "Test", "domain": "structured_diagram", "width": 500, "height": 200},
    }).json()
    command_id = str(uuid4())
    monkeypatch.setattr(routes, "render_diagram", lambda *args: (_ for _ in ()).throw(ValueError("synthetic failure")))
    response = client.post(f"/api/design-sessions/{created['id']}/native-diagram", json={
        "command_id": command_id, "expected_revision": created["revision"],
        "diagram": {"width": 500, "height": 200, "nodes": [{"id": "node_test", "label": "Test", "x": 10, "y": 10}], "edges": []},
    })
    assert response.status_code == 422
    with app.state.session_factory() as session:
        assert session.get(Command, ("owner_local", command_id)).result["render_status"] == "failed"
        reservation = session.get(DesignOperation, "visualrender_" + command_id.replace("-", ""))
        assert reservation.payload["status"] == "released"


def test_repeated_blocked_visual_commands_do_not_break_the_bounded_state_view(image_factory, image_configuration):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        first = session.get(DesignOperation, operation_id)
        first.payload = {**first.payload, "status": "blocked", "error_code": "synthetic_rejection"}
        for index in range(25):
            session.add(DesignOperation(id=f"image_blocked_{index:02}", session_id=design_id, provider="openai",
                                        payload={"kind": "generation", "status": "blocked", "error_code": "synthetic_rejection"}))
        result = DesignSessionView.model_validate(design_view(session, session.get(DesignSession, design_id)))
        assert len(result.operation_failures) == 24
        assert any("most recent" in limitation for limitation in result.limitations)
