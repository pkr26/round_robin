"""Assessment pages expose only run-scoped evidence and keep saved history."""

from copy import deepcopy

from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.database.models import EvidenceRecord, Run, utcnow
from round_robin.research.discussion import assessment_target
from round_robin.schemas.discussion import ScopedAssessment
from sqlalchemy import select

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_assessment_page_scopes_cost_references_and_keeps_history(factory):
    run_id = seed(factory, brief=complete_brief())
    other_id = seed(factory, brief=complete_brief(), suffix="other")
    drain(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        own = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id)))
        other = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == other_id)))
        assert len(own) >= 2 and other
        brief = deepcopy(run.configuration.get("resolved_brief", run.input_snapshot))
        option = brief["options"][0]
        option["cost_components"][0]["source_ref"] = own[0].id
        option["cost_components"][1]["source_ref"] = other[0].id
        option["cost_components"][2]["source_ref"] = "missing_evidence"
        run.configuration = {**run.configuration, "resolved_brief": brief}
        requirement = next(r for r in brief["hard_requirements"] if r["metric"] != "total_cost")
        record = ScopedAssessment(
            id="assessment_view_history", target_type="requirement", option_id=option["id"],
            requirement_id=requirement["id"], target_hash=assessment_target(run, "requirement", option["id"], requirement["id"]),
            reference_ids=[own[0].id], evidence_hashes={own[0].id: own[0].sha256}, result="pass",
            attestation="I attest this exact requirement and referenced evidence.", actor_id=run.owner_id,
            run_id=run.id, recorded_at=utcnow(),
        ).model_dump(mode="json")
        run.configuration = {**run.configuration, "evidence_assessments": [record]}
        own_ids = {row.id for row in own}
        expected_cost_refs = {component["source_ref"] for component in option["cost_components"]} & own_ids
        option_id = option["id"]
        manual_id = requirement["id"]
    app = create_app(str(factory.kw["bind"].url))
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/assessments")
        assert response.status_code == 200, response.text
        page = response.json()
    cost = next(t for t in page["targets"] if t["target_type"] == "option_costs" and t["option_id"] == option_id)
    assert cost["reference_ids"] == sorted(expected_cost_refs)
    manual = next(t for t in page["targets"] if t["target_type"] == "requirement" and t["option_id"] == option_id and t["requirement_id"] == manual_id)
    assert manual["reference_ids"] == sorted(own_ids)
    assert [item["id"] for item in page["items"]] == ["assessment_view_history"]
    assert [item["id"] for item in page["current_items"]] == ["assessment_view_history"]


def test_snapshot_is_used_when_legacy_run_has_no_resolved_brief(factory):
    run_id = seed(factory, brief=complete_brief())
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {key: value for key, value in run.configuration.items() if key != "resolved_brief"}
        option_ids = {option["id"] for option in run.input_snapshot["options"]}
    app = create_app(str(factory.kw["bind"].url))
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/assessments")
        assert response.status_code == 200, response.text
        assert {item["option_id"] for item in response.json()["targets"] if item["target_type"] == "option_costs"} == option_ids
