"""Current attestations remain scoped, dated, and latest-record consistent."""
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from round_robin.database.models import EvidenceRecord, Run
from round_robin.research import discussion
from round_robin.research.claims import ClaimRejected
from round_robin.schemas.discussion import ScopedAssessment
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


def attestation(run, evidence, identity, option_id, valid_until):
    return ScopedAssessment(
        id=identity, target_type="option_costs", option_id=option_id,
        target_hash=discussion.assessment_target(run, "option_costs", option_id),
        reference_ids=[evidence.id], evidence_hashes={evidence.id: evidence.sha256}, result="supported",
        attestation="I attest these exact saved cost inputs.", actor_id=run.owner_id, run_id=run.id,
        recorded_at=datetime(2026, 11, 17, 12, tzinfo=UTC), valid_until=valid_until,
    ).model_dump(mode="json")


@pytest.mark.parametrize("case,expected", [
    ("boundary", ["assessment_current"]),
    ("expired", []),
    ("superseded", []),
    ("missing_target_then_valid", ["assessment_current"]),
    ("expired_then_valid", ["assessment_current"]),
    ("references_mismatch", []),
])
def test_assessment_page_keeps_history_but_only_current_exact_attestations(artifacts, monkeypatch, case, expected):
    app, client, _, run_id, _ = artifacts
    # 02:00 UTC on Nov 18 is still Nov 17 in the frozen Phoenix timezone.
    monkeypatch.setattr("round_robin.api.assessments.utcnow", lambda: datetime(2026, 11, 18, 2, tzinfo=UTC))
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        evidence = session.scalar(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(EvidenceRecord.id))
        options = run.input_snapshot["options"]
        current = attestation(run, evidence, "assessment_current", options[0]["id"], "2026-11-17")
        records = [current]
        if case == "expired":
            current["valid_until"] = "2026-11-16"
        elif case == "superseded":
            old = deepcopy(current)
            old["id"], old["valid_until"] = "assessment_old", "2026-11-18"
            current["valid_until"] = "2026-11-16"
            records = [old, current]
        elif case == "missing_target_then_valid":
            invalid = deepcopy(current)
            invalid["id"], invalid["option_id"] = "assessment_missing", "option_missing"
            records = [invalid, current]
        elif case == "expired_then_valid":
            expired = deepcopy(current)
            expired["id"], expired["valid_until"] = "assessment_expired", "2026-11-16"
            current = attestation(run, evidence, "assessment_current", options[1]["id"], "2026-11-17")
            records = [expired, current]
        elif case == "references_mismatch":
            current["reference_ids"].append("evidence_missing")
        run.configuration = {**run.configuration, "evidence_assessments": records}
        history = [record["id"] for record in records]
    response = client.get(f"/api/runs/{run_id}/assessments")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == history
    assert [item["id"] for item in response.json()["current_items"]] == expected


@pytest.mark.parametrize("field,value", [("actor_id", "other_owner"), ("run_id", "other_run")])
def test_current_assessment_rejects_synthetic_foreign_owner_or_run(artifacts, field, value):
    app, _, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        evidence = session.scalar(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(EvidenceRecord.id))
        record = attestation(run, evidence, "assessment_current", run.input_snapshot["options"][0]["id"], None)
        record[field] = value
        run.configuration = {**run.configuration, "evidence_assessments": [record]}
        with pytest.raises(ClaimRejected, match="ownership"):
            discussion.current_assessments(run, {evidence.id: {"sha256": evidence.sha256}})
