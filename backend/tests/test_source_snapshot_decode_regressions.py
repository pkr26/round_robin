"""Corrupt evidence decoding preserves saved-read and ledger availability semantics."""

import pytest
from round_robin.database.models import Run
from sqlalchemy import text

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.mark.parametrize("raw", ['{"private":"PRIVATE_SOURCE_SENTINEL",', '[' * 1200 + '0' + ']' * 1200, '[' * 10000 + '0' + ']' * 10000])
def test_corrupt_evidence_is_sanitized_on_snapshot_and_versioned_reports(artifacts, raw):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        session.execute(text("UPDATE evidence SET content=:raw WHERE id='passage_offer_a'"), {"raw": raw})
    original = client._transport.raise_server_exceptions
    client._transport.raise_server_exceptions = False
    try:
        for path in (f"/api/runs/{run_id}/snapshot", f"/api/runs/{run_id}/report", f"/api/runs/{run_id}/reports/1"):
            response = client.get(path)
            assert response.status_code == 503, (path, response.text)
            assert response.json()["error"]["code"] == "saved_result_unavailable"
            assert "PRIVATE_SOURCE_SENTINEL" not in response.text
        run = client.get(f"/api/runs/{run_id}")
        assert run.status_code == 200 and run.json()["readiness"] is None
    finally:
        client._transport.raise_server_exceptions = original
    with app.state.session_factory() as session:
        assert session.scalar(text("SELECT content FROM evidence WHERE id='passage_offer_a'")) == raw
        assert session.get(Run, run_id).report_version == report["version"]


def test_ledger_unavailable_snapshot_with_corrupt_evidence_remains_coherent(artifacts):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, "ledger_integrity_failed": True}
        session.execute(text("UPDATE evidence SET content=:raw WHERE id='passage_offer_a'"), {"raw": '{"private":"PRIVATE_SOURCE_SENTINEL",'})
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"] is None and body["evidence"] is None
    assert body["run"]["readiness"] is None
    assert body["run"]["remaining_limits"]["requests"] is None
    assert body["run"]["remaining_limits"]["requests_unavailable_reason"] == "ledger_integrity_failed"
    assert "PRIVATE_SOURCE_SENTINEL" not in response.text
