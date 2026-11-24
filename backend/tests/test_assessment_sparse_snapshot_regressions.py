"""Older sparse saved snapshots still expose valid scoped assessment targets."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.database.models import Run
from round_robin.schemas.briefs import DraftBrief

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("missing", ["options", "cost_components", "hard_requirements"])
def test_sparse_saved_snapshot_keeps_assessment_page_available(factory, missing):
    run_id = seed(factory, brief=complete_brief())
    with factory.begin() as session:
        run = session.get(Run, run_id)
        snapshot = deepcopy(run.input_snapshot)
        if missing == "cost_components":
            snapshot["options"][0].pop("cost_components")
        else:
            snapshot.pop(missing)
        # This is a schema-valid older sparse document, not the normalized
        # representation emitted by current write paths.
        DraftBrief.model_validate(snapshot)
        run.input_snapshot = snapshot
        run.configuration = {key: value for key, value in run.configuration.items() if key != "resolved_brief"}
        option_ids = [option["id"] for option in snapshot.get("options", [])]
    app = create_app(str(factory.kw["bind"].url))
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f"/api/runs/{run_id}/assessments")
    assert response.status_code == 200, response.text
    targets = response.json()["targets"]
    assert [row["option_id"] for row in targets if row["target_type"] == "option_costs"] == option_ids
    if missing == "cost_components":
        assert next(row for row in targets if row["target_type"] == "option_costs" and row["option_id"] == option_ids[0])["reference_ids"] == []
    if missing == "hard_requirements":
        assert all(row["target_type"] == "option_costs" for row in targets)
