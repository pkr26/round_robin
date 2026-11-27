"""Current attestation expiry uses the frozen brief's local date."""
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from round_robin.database.models import EvidenceRecord, Run
from round_robin.api import assessments
from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414
from backend.tests.test_current_assessment_mutation_regressions import attestation


@pytest.mark.parametrize("case,expected", [("resolved", []), ("fallback_zone", ["assessment_current"])])
def test_assessment_expiry_uses_frozen_local_date(artifacts, monkeypatch, case, expected):
    app, client, _, run_id, _ = artifacts
    monkeypatch.setattr(assessments, "utcnow", lambda: datetime(2026, 11, 18, 2, tzinfo=UTC))
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        evidence = session.scalar(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(EvidenceRecord.id))
        brief = deepcopy(run.input_snapshot)
        if case == "resolved":
            # Tokyo is already Nov 18; the original Phoenix input is still Nov 17.
            brief["timezone"] = "Asia/Tokyo"
        else:
            # A sparse saved brief uses the Phoenix fallback.
            brief.pop("timezone", None)
        run.configuration = {**run.configuration, "resolved_brief": brief}
        item = attestation(run, evidence, "assessment_current", brief["options"][0]["id"], "2026-11-17")
        run.configuration = {**run.configuration, "evidence_assessments": [item]}
    response = client.get(f"/api/runs/{run_id}/assessments")
    assert response.status_code == 200, response.text
    assert [record["id"] for record in response.json()["items"]] == ["assessment_current"]
    assert [record["id"] for record in response.json()["current_items"]] == expected
