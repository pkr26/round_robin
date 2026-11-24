from datetime import timedelta

import pytest
from round_robin.database.models import EvidenceRecord, ReportVersion, Run
from sqlalchemy import text

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.mark.parametrize("legacy", [False, True])
def test_unreferenced_corrupt_evidence_at_cutoff_isolated_only_for_explicit_scope(
    artifacts, legacy
):
    app, client, meeting, rid, saved = artifacts
    with app.state.session_factory.begin() as session:
        row = session.get(ReportVersion, saved["id"])
        run = session.get(Run, rid)
        if legacy:
            content = dict(row.content)
            content.pop("evidence_ids")
            row.content = content
        bad = EvidenceRecord(
            id="evidence_unreferenced_bad",
            run_id=rid,
            meeting_id=meeting,
            owner_id=run.owner_id,
            sha256="a" * 64,
            content={},
            created_at=row.created_at - timedelta(seconds=1),
        )
        session.add(bad)
        session.flush()
        session.execute(
            text("UPDATE evidence SET content=:raw WHERE id=:id"),
            {"raw": '{"PRIVATE_SENTINEL":', "id": bad.id},
        )
    response = client.get(f"/api/runs/{rid}/report")
    assert response.status_code == (503 if legacy else 200), response.text
    if not legacy:
        assert response.json()["evidence_ids"] == saved["evidence_ids"]
    assert "PRIVATE_SENTINEL" not in response.text
    snapshot = client.get(f"/api/runs/{rid}/snapshot")
    assert snapshot.status_code == 503 and "PRIVATE_SENTINEL" not in snapshot.text
