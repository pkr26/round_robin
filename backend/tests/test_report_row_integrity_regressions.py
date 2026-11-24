"""Report hydration failures retain existing receipts and block new unchecked actions."""

from uuid import uuid4

import pytest
from round_robin.database.models import Command, ExportRecord, Outcome, Run
from sqlalchemy import func, select, text

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


def test_empty_report_id_uses_current_ledger_gate_but_explicit_history_remains_readable(artifacts):
    app, client, _, run_id, report = artifacts
    calculation_id = report["calculations"][0]["id"]
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "ledger_integrity_failed": True}
    path = f"/api/runs/{run_id}/calculations/{calculation_id}"
    for params in ({}, {"report_id": ""}):
        result = client.get(path, params=params)
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "ledger_integrity_failed"
    explicit = client.get(path, params={"report_id": report["id"]})
    assert explicit.status_code == 200 and explicit.json()["report_id"] == report["id"]


@pytest.mark.parametrize("raw", ['{"private":"PRIVATE_REPORT_SENTINEL",', '[' * 10000 + '0' + ']' * 10000])
def test_corrupt_report_preserves_receipts_and_owner_observations_without_new_export(artifacts, raw):
    app, client, meeting_id, run_id, report = artifacts
    export_body = {"command_id": str(uuid4()), "report_id": report["id"], "format": "json"}
    exported = client.post(f"/api/runs/{run_id}/exports", json=export_body)
    assert exported.status_code == 200, exported.text
    export = exported.json()
    outcome_body = {"command_id": str(uuid4()), "run_id": run_id, "report_version": 1, "choice": "accepted"}
    outcome = client.post(f"/api/meetings/{meeting_id}/outcomes", json=outcome_body)
    assert outcome.status_code == 201, outcome.text
    with app.state.session_factory.begin() as session:
        original = session.scalar(text("SELECT content FROM report_versions WHERE id=:id"), {"id": report["id"]})
        session.execute(text("UPDATE report_versions SET content=:raw WHERE id=:id"), {"raw": raw, "id": report["id"]})
    new_export = {**export_body, "command_id": str(uuid4())}
    new_outcome = {**outcome_body, "command_id": str(uuid4())}
    original_exception_mode = client._transport.raise_server_exceptions
    client._transport.raise_server_exceptions = False
    try:
        status = client.get(f"/api/exports/{export['id']}")
        assert status.status_code == 200 and status.json() == export
        assert client.get(export["download_url"]).status_code == 200
        assert client.post(f"/api/runs/{run_id}/exports", json=export_body).json() == export
        assert client.post(f"/api/meetings/{meeting_id}/outcomes", json=outcome_body).json() == outcome.json()
        path = f"/api/runs/{run_id}/calculations/{report['calculations'][0]['id']}"
        for params in ({}, {"report_id": report["id"]}):
            response = client.get(path, params=params)
            assert response.status_code == 409 and response.json()["error"]["code"] == "report_invalid"
            assert "PRIVATE_REPORT_SENTINEL" not in response.text
        response = client.post(f"/api/runs/{run_id}/exports", json=new_export)
        assert response.status_code == 409 and response.json()["error"]["code"] == "report_invalid"
        response = client.post(f"/api/meetings/{meeting_id}/outcomes", json=new_outcome)
        assert response.status_code == 201, response.text
        assert client.post(f"/api/meetings/{meeting_id}/outcomes", json=new_outcome).json() == response.json()
        assert "PRIVATE_REPORT_SENTINEL" not in response.text
    finally:
        client._transport.raise_server_exceptions = original_exception_mode
    with app.state.session_factory.begin() as session:
        assert session.get(Command, ("owner_local", new_export["command_id"])) is None
        assert session.get(Command, ("owner_local", new_outcome["command_id"])) is not None
        assert session.scalar(select(func.count()).select_from(ExportRecord)) == 1
        assert session.scalar(select(func.count()).select_from(Outcome)) == 2
        session.execute(text("UPDATE report_versions SET content=:raw WHERE id=:id"), {"raw": original, "id": report["id"]})
    assert client.post(f"/api/runs/{run_id}/exports", json=new_export).status_code == 200
    assert client.post(f"/api/meetings/{meeting_id}/outcomes", json=new_outcome).status_code == 201


@pytest.mark.parametrize("operation", ["calculation", "export", "outcome"])
def test_corrupt_report_materialization_preserves_operation_contract(artifacts, operation):
    app, client, meeting_id, run_id, report = artifacts
    command_id = str(uuid4())
    with app.state.session_factory.begin() as session:
        session.execute(text("UPDATE report_versions SET content=:raw WHERE id=:id"), {"raw": '{"private":"PRIVATE_REPORT_SENTINEL",', "id": report["id"]})
    original_exception_mode = client._transport.raise_server_exceptions
    client._transport.raise_server_exceptions = False
    try:
        if operation == "calculation":
            response = client.get(f"/api/runs/{run_id}/calculations/{report['calculations'][0]['id']}")
        elif operation == "export":
            response = client.post(f"/api/runs/{run_id}/exports", json={"command_id": command_id, "report_id": report["id"], "format": "json"})
        else:
            response = client.post(f"/api/meetings/{meeting_id}/outcomes", json={"command_id": command_id, "run_id": run_id, "report_version": 1, "choice": "accepted"})
    finally:
        client._transport.raise_server_exceptions = original_exception_mode
    if operation == "outcome":
        assert response.status_code == 201
    else:
        assert response.status_code == 409 and response.json()["error"]["code"] == "report_invalid"
    assert "PRIVATE_REPORT_SENTINEL" not in response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ExportRecord)) == 0
        assert session.scalar(select(func.count()).select_from(Outcome)) == (1 if operation == "outcome" else 0)
        assert (session.get(Command, ("owner_local", command_id)) is not None) == (operation == "outcome")
