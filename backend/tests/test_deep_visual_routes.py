"""Owner-scoped visual HTTP commands through real adapters and injected transport.

These journeys keep the actual API command/preflight/worker/ledger boundaries;
only the remote provider transport is synthetic. No upstream socket is opened.
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from backend.tests.test_image_provider import image_configuration, pixels
from backend.tests.test_visual import visual_app
from round_robin.database.models import BudgetOperation, Command
from round_robin.database.services import lock_policies
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.orchestration.budget import operations_exposure
from round_robin.providers import images
from round_robin.worker.image_tasks import execute_image_operation


@pytest.fixture
def funded_visual_routes(image_configuration, visual_app, monkeypatch):
    app, client, meeting, _ = visual_app
    app.state.settings = replace(app.state.settings, paid_access_enabled=True)
    with app.state.session_factory.begin() as session:
        deployment, owner = lock_policies(session, "owner_local")
        for bucket in (deployment, owner):
            bucket.spending_locked, bucket.limit_usd = False, "10"
            bucket.metadata_json = {"daily_usd": "10", "monthly_usd": "50", "deployment_epoch": 1}
    requests, hooks = [], {}
    original = images.HTTPImageProvider

    def respond(request):
        payload = json.loads(request.content)
        kind = "edit" if request.url.path.endswith("/edits") else "generation" if request.url.path.endswith("/generations") else "vision"
        requests.append({"kind": kind, "host": request.url.host, "payload": payload})
        if kind in hooks:
            hooks.pop(kind)()
        if kind != "vision":
            output = pixels("blue" if kind == "generation" else "green")
            return httpx.Response(200, headers={"x-request-id": f"synthetic-route-{len(requests)}"}, json={"data": [{"b64_json": base64.b64encode(output).decode()}]})
        content = json.dumps({"findings": [{"id": "finding_local_alias", "description": "Synthetic fixture alignment observation", "category": "observed_defect", "severity": "minor"}], "limitations": ["Synthetic transport; no real quality judgment."]})
        body = {"stop_reason": "end_turn", "content": [{"type": "text", "text": content}]} if request.url.host == "api.anthropic.com" else {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": content}]}}]}
        return httpx.Response(200, headers={"x-request-id": f"synthetic-route-{len(requests)}"}, json=body)

    class TransportBoundAdapter(original):
        def __init__(self, provider, configuration):
            super().__init__(provider, configuration, transport=httpx.MockTransport(respond), api_key="synthetic-route-only")

    monkeypatch.setattr(images, "HTTPImageProvider", TransportBoundAdapter)
    response = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={
        "command_id": str(uuid4()), "mode": "improve", "maximum_usd": "2",
        "recipients": ["openai", "anthropic", "google"],
        "brief": {"purpose": "Synthetic API to durable visual state", "audience": "Test reviewer", "width": 64, "height": 64},
    })
    assert response.status_code == 201, response.text
    return app, client, response.json(), requests, hooks


def prepare(client, design, action):
    response = client.post(f"/api/design-sessions/{design['id']}/preflight", json={"command_id": str(uuid4()), "expected_revision": design["revision"], "action": action})
    assert response.status_code == 200, response.text
    assert response.json()["blockers"] == []
    return response.json()


def improve_body(preflight, *, target=None, dispositions=None):
    return {"command_id": str(uuid4()), "expected_revision": preflight["revision"], "preflight_id": preflight["id"], "target_version_id": target, "intent": "Preserve the exact copy and adjust only the background.", "finding_dispositions": dispositions or {}}


def execute(app, operation):
    # provider_registry is deliberately omitted: this traverses the real
    # configured_image_provider factory and HTTP adapter preflight/parser.
    result = execute_image_operation(operation, session_factory=app.state.session_factory, artifact_directory=app.state.settings.artifact_directory)
    assert result["status"] == "persisted", result


def read(client, design):
    response = client.get(f"/api/design-sessions/{design['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def test_improvement_route_replays_once_checks_owner_revision_findings_and_preserves_history(funded_visual_routes):
    app, client, design, requests, _ = funded_visual_routes
    path = f"/api/design-sessions/{design['id']}/improvements"
    quote = prepare(client, design, "generate")
    body = improve_body(quote)
    app.state.identity_provider = lambda request: "owner_other"
    assert client.post(path, json=body).status_code == 404
    app.state.identity_provider = lambda request: "owner_local"
    accepted = client.post(path, json=body)
    assert accepted.status_code == 202, accepted.text
    original_receipt = accepted.json()
    operation = original_receipt["accepted_operation_ids"][0]
    assert client.post(path, json=body).json() == original_receipt
    assert client.post(path, json={**body, "command_id": str(uuid4())}).status_code == 409
    assert requests == []
    with app.state.session_factory() as session:
        assert len(list(session.scalars(select(DesignOperation).where(DesignOperation.session_id == design["id"])))) == 1
        assert session.scalar(select(BudgetOperation).where(BudgetOperation.run_id == design["id"])) is None
    execute(app, operation)
    first = read(client, design)
    original_version = deepcopy(first["versions"][0])
    assert original_version["coverage"]["complete"] is False
    original_bytes = client.get(original_version["image_url"]).content
    for queued in first["pending_operations"]:
        execute(app, queued["id"])
    reviewed = read(client, design)
    original_reviewed = deepcopy(reviewed["versions"][0])
    assert original_reviewed["coverage"]["complete"] is True
    findings = [finding["id"] for review in original_reviewed["reviews"] for finding in review["findings"]]
    assert len(set(findings)) == len(findings) == 2
    edit_quote = prepare(client, reviewed, "edit")
    edit_body = improve_body(edit_quote, target=original_reviewed["id"], dispositions={"finding_foreign": "address"})
    invalid = client.post(path, json=edit_body)
    assert invalid.status_code == 409 and invalid.json()["error"]["code"] == "invalid_finding"
    # Invalid dispositions did not consume the displayed preflight or queue I/O.
    edit_body = improve_body(edit_quote, target=original_reviewed["id"], dispositions={findings[0]: "address", findings[1]: "decline"})
    edit = client.post(path, json=edit_body)
    assert edit.status_code == 202, edit.text
    assert client.post(path, json=edit_body).json() == edit.json()
    edit_operation = edit.json()["accepted_operation_ids"][0]
    execute(app, edit_operation)
    second = read(client, design)
    assert second["versions"][0] == original_reviewed
    assert client.get(original_reviewed["image_url"]).content == original_bytes
    assert second["versions"][1]["parent_version_id"] == original_reviewed["id"]
    assert second["versions"][1]["provenance"]["parent_hash"] == original_reviewed["canonical_hash"]
    assert second["versions"][1]["canonical_hash"] != original_reviewed["canonical_hash"]
    assert second["state"]["request_count"] == 4 and second["state"]["submission_count"] == 2
    assert second["state"]["intentional_edits"] == 1
    edit_request = next(request for request in requests if request["kind"] == "edit")
    assert edit_request["payload"]["images"][0]["image_url"] == "data:image/png;base64," + base64.b64encode(original_bytes).decode()
    assert json.loads(edit_request["payload"]["prompt"])["targeted_changes"] == edit_body["intent"]
    with app.state.session_factory() as session:
        original = session.get(Command, ("owner_local", body["command_id"]))
        assert original.result == original_receipt
        assert operations_exposure(list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == design["id"])))) == Decimal("1.20")
    # A stale or foreign cancellation cannot retire this owner's current work.
    cancel_path = f"/api/design-sessions/{design['id']}/cancel"
    cancel_body = {"command_id": str(uuid4()), "expected_revision": second["revision"]}
    assert client.post(cancel_path, json={**cancel_body, "command_id": str(uuid4()), "expected_revision": 1}).status_code == 409
    app.state.identity_provider = lambda request: "owner_other"
    assert client.post(cancel_path, json=cancel_body).status_code == 404
    app.state.identity_provider = lambda request: "owner_local"
    cancelled = client.post(cancel_path, json=cancel_body)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"]["fence"] == second["state"]["fence"] + 1
    assert cancelled.json()["versions"] == second["versions"]
    assert client.post(cancel_path, json=cancel_body).json() == cancelled.json()
    before = len(requests)
    for queued in second["pending_operations"]:
        result = execute_image_operation(queued["id"], session_factory=app.state.session_factory, artifact_directory=app.state.settings.artifact_directory)
        assert result["status"] == "blocked"
    assert len(requests) == before == 4
    assert read(client, design)["state"]["request_count"] == 4
    with app.state.session_factory() as session:
        hold = session.get(BudgetOperation, "visualfinish_" + design["id"])
        assert hold.status == "released" and Decimal(hold.reserved_usd) == 0
        assert operations_exposure(list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == design["id"])))) == Decimal("1.00")


@pytest.mark.parametrize("change", ["missing", "reconfigured"])
def test_operator_configuration_change_after_displayed_preflight_cannot_queue_work(funded_visual_routes, change):
    app, client, design, requests, _ = funded_visual_routes
    quote = prepare(client, design, "generate")
    configuration = Path(os.environ["ROUND_ROBIN_IMAGE_CONFIG_FILE"])
    if change == "missing":
        configuration.unlink()
    else:
        packet = json.loads(configuration.read_text())
        packet["quality"] = "high"
        configuration.write_text(json.dumps(packet))
    response = client.post(f"/api/design-sessions/{design['id']}/improvements", json=improve_body(quote))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "visual_configuration_changed"
    assert requests == []
    with app.state.session_factory() as session:
        saved = session.get(DesignSession, design["id"])
        assert saved.preflight["consumed_by_command"] is None and saved.revision == design["revision"]
        assert saved.state["request_count"] == saved.state["submission_count"] == 0
        assert session.scalar(select(DesignOperation).where(DesignOperation.session_id == design["id"])) is None
        assert session.scalar(select(BudgetOperation).where(BudgetOperation.run_id == design["id"])) is None


def test_cancellation_via_http_during_real_adapter_send_fences_pixels_but_preserves_unknown_charge(funded_visual_routes):
    app, client, design, requests, hooks = funded_visual_routes
    quote = prepare(client, design, "generate")
    body = improve_body(quote)
    path = f"/api/design-sessions/{design['id']}/improvements"
    accepted = client.post(path, json=body)
    assert accepted.status_code == 202, accepted.text
    operation = accepted.json()["accepted_operation_ids"][0]
    observed = {}
    def cancel_during_send():
        current = read(client, design)
        assert current["state"]["request_count"] == current["state"]["submission_count"] == 1
        response = client.post(f"/api/design-sessions/{design['id']}/cancel", json={"command_id": str(uuid4()), "expected_revision": current["revision"]})
        assert response.status_code == 200, response.text
        observed.update(response.json())
    hooks["generation"] = cancel_during_send
    execute(app, operation)
    final = read(client, design)
    assert final["status"] == "cancelled" and final["versions"] == []
    assert final["state"]["fence"] == observed["state"]["fence"] == 1
    assert final["state"]["request_count"] == final["state"]["submission_count"] == 1
    assert len(requests) == 1
    assert client.post(path, json=body).json() == accepted.json()
    with app.state.session_factory() as session:
        sent = session.get(DesignOperation, operation)
        assert sent.payload["status"] == "fenced"
        assert session.get(BudgetOperation, operation).status == "unknown"
        assert operations_exposure(list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == design["id"])))) == Decimal("0.40")
        assert session.scalar(select(DesignVersion).where(DesignVersion.session_id == design["id"])) is None
        storage = session.get(DesignOperation, sent.payload["storage_reservation_id"])
        assert storage.payload["status"] == "released"
    assert list((app.state.settings.artifact_directory / "visual").rglob("*.png")) == []
