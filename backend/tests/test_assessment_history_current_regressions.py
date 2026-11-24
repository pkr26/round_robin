"""Assessment views preserve empty history and independent current targets."""
import pytest
from round_robin.database.models import EvidenceRecord, Run
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414
from backend.tests.test_current_assessment_mutation_regressions import attestation


def test_assessment_page_without_saved_assessment_history(artifacts):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory() as session:
        run = session.get(Run, run_id)
        assert "evidence_assessments" not in run.configuration
    response = client.get(f"/api/runs/{run_id}/assessments")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["current_items"] == []


@pytest.mark.parametrize("artifacts", [False], indirect=True)
def test_assessment_page_keeps_current_attestations_for_distinct_options(artifacts):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        evidence = session.scalar(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(EvidenceRecord.id))
        options = run.input_snapshot["options"]
        records = [
            attestation(run, evidence, f"assessment_{i}", option["id"], None)
            for i, option in enumerate(options[:2])
        ]
        assert len(records) == 2
        run.configuration = {**run.configuration, "evidence_assessments": records}
    response = client.get(f"/api/runs/{run_id}/assessments")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == ["assessment_0", "assessment_1"]
    assert [item["id"] for item in response.json()["current_items"]] == ["assessment_0", "assessment_1"]
