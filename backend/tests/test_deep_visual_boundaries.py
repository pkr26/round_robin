"""Independent adversarial visual contracts; all provider and renderer I/O is mocked."""
from dataclasses import asdict
from decimal import Decimal
import base64
import hashlib
from io import BytesIO
import json
from uuid import uuid4

import httpx
from hypothesis import given, settings, strategies as st
from PIL import Image
import pytest
from sqlalchemy import select

from backend.tests.test_image_provider import authorization, generation_packet, image_configuration
from backend.tests.test_image_worker import FakeImageAdapter, image_factory, seed_image
from backend.tests.test_visual import visual_app
from round_robin.database.models import BudgetOperation, Command
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import HTTPImageProvider
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.renderers import interface
from round_robin.visual.renderers.interface import NativeRendererConfiguration, NativeRendererError
from round_robin.worker.image_tasks import execute_image_operation


@pytest.mark.parametrize("payload", [None, [], "unexpected", {"data": [None]}, {"data": [42]}, {"data": [[]]}])
def test_image_malformed_success_is_typed_private_and_never_retried(image_configuration, payload):
    sent = []
    def response(request):
        sent.append(request)
        return httpx.Response(200, headers={"x-request-id": "request_bad_shape"}, content=json.dumps(payload))
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(response), api_key="synthetic-only")
    with pytest.raises(ProviderFailure) as failure:
        adapter.submit(generation_packet(), authorization(), idempotency_key=None)
    assert failure.value.code == "invalid_image_result"
    assert failure.value.provider_request_id == "request_bad_shape"
    assert failure.value.retryable is False
    assert len(sent) == 1


def renderer_reply(packet, *, changes=None):
    output = BytesIO()
    Image.new("RGB", (packet["viewport"]["width"], packet["viewport"]["height"]), "white").save(output, format="PNG")
    png = output.getvalue()
    return {"schema_version": "1.0", "renderer_version": interface.RENDERER_VERSION,
            "playwright_version": "synthetic", "browser_version": "synthetic",
            "source_sha256": packet["source_sha256"], "configuration_hash": packet["configuration_hash"],
            "viewport": packet["viewport"], "screenshot_sha256": hashlib.sha256(png).hexdigest(),
            "screenshot_base64": base64.b64encode(png).decode(),
            "checks": [{"index": index, "action": action["action"],
                        "selector_sha256": hashlib.sha256(action["selector"].encode()).hexdigest(),
                        "status": "passed", "assertion": "passed" if action.get("assertion") else "not_requested"}
                       for index, action in enumerate(packet["actions"])],
            "network_attempts": [], "network_attempt_count": 0, "page_error_count": 0, "limitations": [],
            **(changes or {})}


def mock_renderer(monkeypatch, change):
    invocations = []
    def process(arguments, content, **limits):
        invocations.append(arguments)
        if arguments[:3] == ["docker", "image", "inspect"]:
            return 0, ("sha256:" + "a" * 64).encode(), b""
        if arguments[:2] == ["docker", "rm"]:
            return 0, b"", b""
        packet = json.loads(content)
        reply = renderer_reply(packet)
        change(reply)
        return 0, json.dumps(reply).encode(), b""
    monkeypatch.setattr(interface, "_bounded_process", process)
    return invocations


@pytest.mark.parametrize("status,assertion", [("passed", "not_requested"), ("passed", "unavailable"), ("passed", "failed"), ("failed", "passed")])
def test_native_receipt_cannot_skip_or_contradict_requested_assertion(monkeypatch, status, assertion):
    def change(reply):
        reply["checks"][0].update(status=status, assertion=assertion)
    invocations = mock_renderer(monkeypatch, change)
    with pytest.raises(NativeRendererError, match="provenance"):
        interface.render_interface("<button id='run'>Run</button>", (100, 100),
            [{"action": "click", "selector": "#run", "assertion": {"selector": "#run", "text": "Run"}}],
            configuration=NativeRendererConfiguration(enabled=True, image="synthetic-local"))
    assert len([args for args in invocations if args[:2] == ["docker", "run"]]) == 1
    assert invocations[-1][:3] == ["docker", "rm", "--force"]


def test_native_network_receipt_count_must_match_bounded_recorded_attempts(monkeypatch):
    mock_renderer(monkeypatch, lambda reply: reply.update(network_attempt_count=1))
    with pytest.raises(NativeRendererError, match="provenance"):
        interface.render_interface("<p>Pixels</p>", (100, 100), configuration=NativeRendererConfiguration(enabled=True, image="synthetic-local"))


def test_generation_intent_reaches_the_exact_once_funded_provider_packet(image_factory, image_configuration, tmp_path):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    intent = "PRIVATE_EXACT_INTENT: preserve the copy; make the background blue."
    with image_factory.begin() as session:
        operation = session.get(DesignOperation, operation_id)
        operation.payload = {**operation.payload, "edit_intent": intent}
    adapter = FakeImageAdapter("openai", image_configuration, image_factory)
    assert execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)["status"] == "persisted"
    assert adapter.packets[0].get("edit_intent") == intent
    assert adapter.calls == 1
    with image_factory() as session:
        version = session.scalar(select(DesignVersion))
        assert version.provenance["edit_intent"] == intent
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        state = session.get(DesignSession, design_id).state
        assert state["submission_count"] == state["request_count"] == 1


@pytest.mark.parametrize("mutation", [
    {"poll_count": 0.5}, {"fence": True}, {"epoch": 1.5},
    {"operations": {"rogue": {"kind": "unrecognized", "step_id": "rogue", "bound": "0", "actual": None, "status": "authorized", "authorized_at": 1000}}},
])
def test_corrupt_visual_journal_cannot_restore_execution_authority(mutation):
    with pytest.raises(VisualLimit):
        VisualState(**{**asdict(VisualState("improve", "2", 1000)), **mutation})


def test_unknown_operation_kind_cannot_bypass_shared_call_counters():
    state = VisualState("improve", "2", 1000)
    with pytest.raises(VisualLimit):
        state.authorize("rogue", "rogue", "unrecognized", "0", now=1000, expected_fence=0, epoch=1)
    assert state.operations == {} and state.request_count == 0


@given(st.lists(st.integers(min_value=0, max_value=1000), min_size=1, max_size=24))
@settings(max_examples=35)
def test_unknown_usage_and_restarts_conserve_every_counted_attempt(bounds):
    state = VisualState("improve", "100", 1000)
    exposure = Decimal("0")
    for index, cents in enumerate(bounds):
        bound = Decimal(cents) / 100
        if exposure + bound > 100:
            break
        identity = f"operation_{index}"
        state.authorize(identity, identity, "vision", str(bound), now=1000, expected_fence=0, epoch=1, review_calls=0)
        state.settle(identity, None)
        exposure += bound
        state = VisualState(**asdict(state))
        assert state.request_count == index + 1
        assert state.exposure() == exposure
        assert all(operation["status"] == "unknown" for operation in state.operations.values())


@pytest.mark.parametrize("domain", ["structured_diagram", "native_interface"])
def test_native_conflict_releases_hold_and_terminalizes_original_receipt(visual_app, monkeypatch, domain):
    from types import SimpleNamespace
    from round_robin.api import visual as routes
    from round_robin.visual.renderers.diagram import render_diagram as real_diagram
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED", "1")
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_RENDERER_IMAGE", "synthetic-local")
    created = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={"command_id": str(uuid4()), "mode": "improve", "brief": {"purpose": "Conflict fixture", "audience": "Test", "domain": domain, "width": 500, "height": 200}}).json()
    def conflict(*args, **kwargs):
        with app.state.session_factory.begin() as session:
            design = session.get(DesignSession, created["id"])
            design.revision += 1
        if domain == "structured_diagram":
            return real_diagram(args[0])
        return SimpleNamespace()
    monkeypatch.setattr(routes, "render_diagram" if domain == "structured_diagram" else "render_interface", conflict)
    command = str(uuid4())
    diagram = {"width": 500, "height": 200, "nodes": [{"id": "node_one", "label": "Input", "x": 20, "y": 20}]}
    body = {"command_id": command, "expected_revision": created["revision"], **({"diagram": diagram} if domain == "structured_diagram" else {"source": "<p>Private source</p>"})}
    path = f"/api/design-sessions/{created['id']}/native-{'diagram' if domain == 'structured_diagram' else 'interface'}"
    response = client.post(path, json=body)
    assert response.status_code == 409
    with app.state.session_factory() as session:
        reservation = session.get(DesignOperation, "visualrender_" + command.replace("-", ""))
        assert reservation.payload["status"] == "released"
        assert session.get(Command, ("owner_local", command)).result["render_status"] == "failed"
        assert session.scalar(select(DesignVersion)) is None
    replay = client.post(path, json=body)
    assert replay.status_code == 201 and replay.json()["render_status"] == "failed"


def test_disconnected_text_upload_records_terminal_failure_without_retaining_private_bytes(visual_app):
    import asyncio
    from fastapi import HTTPException
    from starlette.requests import Request
    from round_robin.api.artifacts import upload_text
    from round_robin.database.models import Upload
    app, _, meeting, _ = visual_app
    command = uuid4()
    content = b"Private incomplete text"
    async def disconnected():
        return {"type": "http.disconnect"}
    request = Request({"type": "http", "method": "POST", "path": f"/api/meetings/{meeting['id']}/uploads",
                       "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(content)).encode())],
                       "app": app}, disconnected)
    with app.state.session_factory() as session:
        with pytest.raises(HTTPException) as failure:
            asyncio.run(upload_text(meeting["id"], request, command, 1, "private.txt", hashlib.sha256(content).hexdigest(), "owner_local", session))
        assert failure.value.status_code == 422
    with app.state.session_factory() as session:
        row = session.scalar(select(Upload))
        assert row.status == "failed" and row.canonical_text is None and row.byte_size == 0
        assert session.get(Command, ("owner_local", str(command))).result["status"] == "failed"


def test_native_source_failure_rolls_back_pixels_and_removes_only_new_orphans(visual_app, monkeypatch):
    from pathlib import Path
    from round_robin.api import visual as routes
    app, client, meeting, _ = visual_app
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    created = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json={"command_id": str(uuid4()), "mode": "improve", "brief": {"purpose": "Atomic source fixture", "audience": "Test", "domain": "structured_diagram", "width": 500, "height": 200}}).json()
    path = f"/api/design-sessions/{created['id']}/native-diagram"
    source = {"width": 500, "height": 200, "nodes": [{"id": "node_one", "label": "Initial", "x": 20, "y": 20}]}
    first = client.post(path, json={"command_id": str(uuid4()), "expected_revision": created["revision"], "diagram": source}).json()
    with app.state.session_factory() as session:
        saved = session.scalar(select(DesignVersion))
        retained = {Path(saved.asset_path), Path(saved.provenance["svg_path"]), Path(saved.provenance["source_path"])}
        before = {item: item.read_bytes() for item in retained}
    actual_save = routes.save_native_sources
    def fail_after_save(*args):
        actual_save(*args)
        raise VisualLimit("Synthetic post-source publication conflict")
    monkeypatch.setattr(routes, "save_native_sources", fail_after_save)
    source["nodes"][0]["label"] = "Changed"
    command = str(uuid4())
    failed = client.post(path, json={"command_id": command, "expected_revision": first["revision"], "diagram": source})
    assert failed.status_code == 409
    with app.state.session_factory() as session:
        assert len(list(session.scalars(select(DesignVersion)))) == 1
        state = session.get(DesignSession, created["id"]).state
        assert state["version_count"] == 1 and state["renderer_attempts"] == 2
        assert session.get(Command, ("owner_local", command)).result["render_status"] == "failed"
    namespace = app.state.settings.artifact_directory / "visual" / "owner_local" / created["id"]
    assert set(namespace.iterdir()) == retained
    assert all(item.read_bytes() == before[item] for item in retained)
