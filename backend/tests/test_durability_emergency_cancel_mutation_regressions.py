"""Durability gates preserve reads and the explicit emergency-stop paths."""
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from round_robin.api.auth import require_owner
from round_robin.database.models import BudgetOperation, PolicyBucket, Run, utcnow
from round_robin.database.services import lock_policies
from round_robin.database.visual_models import DesignSession
from round_robin.operations.durability import control_path
from round_robin.security.hosted import digest
from sqlalchemy import select

from backend.tests.test_api import command, start
from backend.tests.test_hosted import hosted as hosted  # noqa: PLC0414
from backend.tests.test_hosted import session_client
from backend.tests.test_visual import visual_app as visual_app  # noqa: PLC0414


@pytest.mark.parametrize("gate", ["independent_marker", "database_policy"])
def test_durability_failure_blocks_writes_but_allows_both_emergency_stops(visual_app, gate):
    app, client, meeting, design = visual_app
    response = start(client, meeting)
    assert response.status_code == 202, response.text
    run = response.json()
    with app.state.session_factory.begin() as session:
        session.add(BudgetOperation(id="synthetic_unknown_text", run_id=run["id"], owner_id="owner_local", kind="model", status="unknown", reserved_usd="0.42", metadata_json={"model_call": True}))
        session.add(BudgetOperation(id="synthetic_unknown_visual", run_id=design["id"], owner_id="owner_local", kind="visual_edit", status="unknown", reserved_usd="0.42", metadata_json={}))
        if gate == "database_policy":
            policy, _ = lock_policies(session, "owner_local")
            policy.metadata_json = {**policy.metadata_json, "durability_compromised": True}
    if gate == "independent_marker":
        directory = control_path(app.state.settings.artifact_directory, app.state.settings.durability_control_directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory/"failure_synthetic.json").write_text(json.dumps({"code": "artifact_storage_failed", "timestamp": utcnow().isoformat()}))

    assert client.get(f"/api/runs/{run['id']}").status_code == 200
    assert client.get(f"/api/design-sessions/{design['id']}").status_code == 200
    blocked = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1))
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "durability_compromised"
    blocked_retry = client.post(f"/api/runs/{run['id']}/retries", json=command(expected_run_version=run["version"], stage="audit"))
    assert blocked_retry.status_code == 503 and blocked_retry.json()["error"]["code"] == "durability_compromised"

    text_command = command(expected_run_version=run["version"])
    stopped_text = client.post(f"/api/runs/{run['id']}/cancel", json=text_command)
    assert stopped_text.status_code == 200 and stopped_text.json()["status"] == "cancel_requested"
    assert client.post(f"/api/runs/{run['id']}/cancel", json=text_command).json() == stopped_text.json()
    visual_command = {"command_id": str(uuid4()), "expected_revision": design["revision"]}
    stopped_visual = client.post(f"/api/design-sessions/{design['id']}/cancel", json=visual_command)
    assert stopped_visual.status_code == 200 and stopped_visual.json()["status"] == "cancelled"
    assert client.post(f"/api/design-sessions/{design['id']}/cancel", json=visual_command).json() == stopped_visual.json()
    with app.state.session_factory() as session:
        assert session.get(Run, run["id"]).execution_fence == 1
        assert session.get(DesignSession, design["id"]).execution_fence == 1
        assert session.get(BudgetOperation, "synthetic_unknown_text").status == "unknown"
        assert session.get(BudgetOperation, "synthetic_unknown_visual").status == "unknown"


def test_independent_durability_marker_blocks_hosted_logout_write(hosted):
    client, token, csrf, _ = session_client(hosted)
    directory = control_path(hosted.state.settings.artifact_directory, hosted.state.settings.durability_control_directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory/"failure_synthetic.json").write_text(json.dumps({"code": "artifact_storage_failed", "timestamp": utcnow().isoformat()}))
    response = client.post("/auth/logout", headers={"x-csrf-token": csrf, "origin": "https://app.example"}, follow_redirects=False)
    assert response.status_code == 503 and response.json()["error"]["code"] == "durability_compromised"
    with hosted.state.session_factory() as session:
        saved = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "session:" + digest(token)))
        assert saved is not None and saved.metadata_json.get("revoked") is not True
    assert client.get("/auth/session").status_code == 200


def test_authentication_guard_itself_blocks_non_cancel_run_write_when_marker_is_set(visual_app):
    app, client, meeting, _ = visual_app
    response = start(client, meeting)
    assert response.status_code == 202
    directory = control_path(app.state.settings.artifact_directory, app.state.settings.durability_control_directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory/"failure_synthetic.json").write_text(json.dumps({"code": "artifact_storage_failed", "timestamp": utcnow().isoformat()}))
    request = Request({"type": "http", "method": "POST", "path": f"/api/runs/{response.json()['id']}/retries", "query_string": b"", "headers": [], "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1234), "app": app})
    with pytest.raises(HTTPException) as error:
        require_owner(request)
    assert error.value.status_code == 503 and error.value.detail["code"] == "durability_compromised"
