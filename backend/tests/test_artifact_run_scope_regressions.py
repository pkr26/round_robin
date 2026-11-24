"""Private artifact routes refuse run authority inconsistent with the owner."""
from uuid import uuid4

import pytest
from round_robin.api.auth import require_owner
from round_robin.database.models import Command, ExportRecord, Run
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


def paths(run_id, report):
    return [f"/api/runs/{run_id}/sources", f"/api/runs/{run_id}/sources/passage_offer_a",
            f"/api/runs/{run_id}/measurements", f"/api/runs/{run_id}/calculations/{report['calculations'][0]['id']}"]


@pytest.mark.parametrize("artifacts", [False], indirect=True)
def test_foreign_owner_receives_same_stable_not_found_on_artifact_routes(artifacts):
    app, client, _, run_id, report = artifacts
    for path in paths(run_id, report):
        response = client.get(path)
        assert response.status_code == 200, response.text
    identity = str(uuid4())
    app.dependency_overrides[require_owner] = lambda: "other_owner"
    try:
        for path in paths(run_id, report):
            response = client.get(path)
            assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
        response = client.post(f"/api/runs/{run_id}/exports", json={"command_id": identity, "report_id": report["id"], "format": "json"})
        assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    finally:
        app.dependency_overrides.clear()
    with app.state.session_factory() as session:
        assert session.get(Command, ("other_owner", identity)) is None
        assert not list(session.scalars(select(ExportRecord)))


def test_inconsistent_run_owner_cannot_be_laundered_through_owned_meeting(artifacts):
    app, client, _, run_id, report = artifacts
    assert client.get(f"/api/runs/{run_id}/sources").status_code == 200
    with app.state.session_factory.begin() as session:
        session.get(Run, run_id).owner_id = "other_owner"
    for path in paths(run_id, report):
        response = client.get(path)
        assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("artifacts", [False], indirect=True)
def test_deleted_meeting_rejects_artifact_reads(artifacts):
    _, client, meeting_id, run_id, report = artifacts
    response = client.request("DELETE", f"/api/meetings/{meeting_id}", json={"command_id": str(uuid4())})
    assert response.status_code == 200
    for path in paths(run_id, report):
        response = client.get(path)
        assert response.status_code == 410 and response.json()["error"]["code"] == "command_resource_gone"
