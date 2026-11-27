"""An invalid earlier attestation must not hide another target's valid one."""
from datetime import UTC, datetime

import pytest
from round_robin.database.models import EvidenceRecord, Run
from round_robin.api import assessments
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414
from backend.tests.test_current_assessment_mutation_regressions import attestation


@pytest.mark.parametrize("invalid_kind", ["evidence_digest", "reference_ids"])
def test_invalid_earlier_assessment_does_not_hide_other_target(artifacts, monkeypatch, invalid_kind):
    app, client, _, run_id, _ = artifacts
    monkeypatch.setattr(assessments, "utcnow", lambda: datetime(2026, 11, 18, 2, tzinfo=UTC))
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        evidence = session.scalar(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(EvidenceRecord.id))
        options = run.input_snapshot["options"]
        invalid = attestation(run, evidence, "assessment_invalid", options[0]["id"], None)
        valid = attestation(run, evidence, "assessment_valid", options[1]["id"], None)
        if invalid_kind == "evidence_digest":
            invalid["evidence_hashes"][evidence.id] = "0" * 64
        else:
            invalid["reference_ids"].append("evidence_missing")
        run.configuration = {**run.configuration, "evidence_assessments": [invalid, valid]}
    response = client.get(f"/api/runs/{run_id}/assessments")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == ["assessment_invalid", "assessment_valid"]
    assert [item["id"] for item in response.json()["current_items"]] == ["assessment_valid"]
