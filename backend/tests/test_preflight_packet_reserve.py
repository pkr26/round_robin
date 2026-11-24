"""Preflight and Start agree on the initial packet's reused-item reserve."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from round_robin.database.models import Upload
from round_robin.providers.base import ProviderRequest

from backend.tests.test_api import application as application  # noqa: PLC0414
from backend.tests.test_api import client as client  # noqa: PLC0414
from backend.tests.test_api import command, create, preflight, start
from backend.tests.test_exhaustive_evidence_reuse import parent as parent  # noqa: PLC0414
from backend.tests.test_exhaustive_evidence_reuse import preflight as reuse_preflight
from backend.tests.test_exhaustive_evidence_reuse import start as reuse_start
from backend.tests.test_operations import prepared as prepared  # noqa: PLC0414


def test_preflight_and_start_allow_normal_input_without_reuse(client, application):
    meeting = create(client)
    quote = preflight(client, meeting)
    assert quote["blockers"] == []
    response = start(client, meeting, quote)
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("target,admitted", [(47500, True), (47501, False)])
def test_reused_evidence_actual_48000_byte_capacity(parent, monkeypatch, target, admitted):
    app, meeting, _, initial_revision, selections, *_ = parent
    original = ProviderRequest.packet
    sizes = []

    def capture(request):
        packet = original(request)
        # Calibrate input against the stable proposal role and future run-ID
        # format, without altering the packet consumed by preflight.
        canonical = {**packet, "role": "openai", "evidence": [
            {**item, "run_id": "run_" + "0" * 32} if item.get("run_id") == request.run_id else item
            for item in packet["evidence"]
        ]}
        sizes.append(len(json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()))
        return packet

    monkeypatch.setattr(ProviderRequest, "packet", capture)
    revision = initial_revision
    brief = meeting["draft"]
    length = 12000
    with TestClient(app) as api:
        for _ in range(30):
            data = b"x" * length
            uploaded = api.post(f"/api/meetings/{meeting['id']}/uploads", content=data, headers={
                "Content-Type": "text/plain", "X-Command-Id": command()["command_id"],
                "X-Expected-Revision": str(revision), "X-Filename": "bounded.txt",
                "X-Content-SHA256": hashlib.sha256(data).hexdigest()})
            assert uploaded.status_code == 201, uploaded.text
            brief = {**brief, "selected_attachment_ids": [uploaded.json()["id"]]}
            patched = api.patch(f"/api/meetings/{meeting['id']}/draft", json=command(expected_revision=revision, brief=brief))
            assert patched.status_code == 200, patched.text
            revision = patched.json()["revision"]
            current_parent = (app, meeting, parent[2], revision, selections, *parent[5:])
            sizes.clear()
            response = reuse_preflight(api, current_parent, selections[:1])
            assert response.status_code == 200, response.text
            assert sizes, (length, response.text)
            difference = target - sizes[-1]
            if difference == 0:
                break
            # Release the prior temporary fixture upload before probing the next
            # length; owner-scoped upload quotas remain enabled.
            with app.state.session_factory.begin() as session:
                session.delete(session.get(Upload, uploaded.json()["id"]))
            length += difference if abs(difference) < 1000 else difference // 2
        else:
            pytest.fail(f"Could not reach target: {length=} {sizes[-1]=} {target=}")
        quote = response.json()
        assert bool(quote["blockers"]) is not admitted
        started, _ = reuse_start(api, current_parent, quote)
        assert started.status_code == (202 if admitted else 409), started.text
        if not admitted:
            assert started.json()["error"]["code"] == "context_limit_downstream_reserve"
