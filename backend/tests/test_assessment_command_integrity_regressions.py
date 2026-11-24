"""Owner attestation commands must stay scoped and atomic without inference."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.database.models import (
    Command,
    EvidenceRecord,
    ReportVersion,
    Run,
    Step,
    utcnow,
)
from sqlalchemy import func, select

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


@pytest.fixture
def attestation(factory):
    run_id = seed(factory, brief=complete_brief())
    drain(factory)
    app = create_app(str(factory.kw["bind"].url))
    app.dependency_overrides[require_owner] = lambda: "owner_test"
    with TestClient(app) as client:
        yield factory, run_id, client


def snapshot(db_factory, run_id):
    with db_factory() as session:
        run = session.get(Run, run_id)
        return (run.version, run.report_version, deepcopy(run.configuration.get("evidence_assessments", [])),
                session.scalar(select(func.count()).select_from(Command)),
                session.scalar(select(func.count()).select_from(ReportVersion)),
                sum(session.scalars(select(Step.calls).where(Step.run_id == run_id))),
                {row.id: deepcopy(row.content) for row in session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id))})


def payload(client, run_id, kind="option_costs"):
    view = client.get(f"/api/runs/{run_id}/assessments")
    assert view.status_code == 200, view.text
    page = view.json()
    target = next(item for item in page["targets"] if item["target_type"] == kind)
    assert len(target["reference_ids"]) >= 2
    return {"command_id": str(uuid4()), "expected_run_version": page["run_version"],
            **{key: target[key] for key in ("target_type", "option_id", "requirement_id", "target_hash", "reference_ids")},
            "result": "supported" if kind == "option_costs" else "pass",
            "attestation": "I attest the exact supplied inputs and referenced evidence for this comparison.", "valid_until": None}


@pytest.mark.parametrize("case,code", [
    ("stale_version", "run_version_conflict"),
    ("active_run", "assessment_run_active"),
    ("changed_target", "assessment_target_changed"),
    ("foreign_reference", "assessment_reference_invalid"),
    ("duplicate_reference", "assessment_reference_invalid"),
    ("expired_attestation", "assessment_expired"),
    ("missing_cost_source", "assessment_cost_incomplete"),
    ("wrong_cost_result", "assessment_cost_incomplete"),
    ("expired_quote", "assessment_quote_expired"),
    ("wrong_requirement_result", "assessment_result_invalid"),
])
def test_invalid_attestation_is_atomic_and_never_invokes_providers(attestation, case, code):
    db_factory, run_id, client = attestation
    if case == "expired_quote":
        with db_factory.begin() as session:
            run = session.get(Run, run_id)
            brief = deepcopy(run.configuration["resolved_brief"])
            brief["options"][0]["cost_components"][0]["quote_valid_until"] = (utcnow().date() - timedelta(days=2)).isoformat()
            run.configuration = {**run.configuration, "resolved_brief": brief}
    body = payload(client, run_id, "requirement" if case == "wrong_requirement_result" else "option_costs")
    if case == "stale_version": body["expected_run_version"] -= 1
    if case == "active_run":
        with db_factory.begin() as session: session.get(Run, run_id).status = "running"
    if case == "changed_target": body["target_hash"] = "0" * 64
    if case == "foreign_reference": body["reference_ids"].append("evidence_foreign")
    if case == "duplicate_reference": body["reference_ids"].append(body["reference_ids"][0])
    if case == "expired_attestation": body["valid_until"] = (utcnow().date() - timedelta(days=2)).isoformat()
    if case == "missing_cost_source": body["reference_ids"] = body["reference_ids"][:-1]
    if case == "wrong_cost_result": body["result"] = "fail"
    if case == "wrong_requirement_result": body["result"] = "supported"
    before = snapshot(db_factory, run_id)
    response = client.post(f"/api/runs/{run_id}/assessments", json=body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == code
    assert snapshot(db_factory, run_id) == before


def test_manual_requirement_attestation_and_replay_are_scoped_without_new_provider_calls(attestation):
    db_factory, run_id, client = attestation
    body = payload(client, run_id, "requirement")
    before = snapshot(db_factory, run_id)
    response = client.post(f"/api/runs/{run_id}/assessments", json=body)
    assert response.status_code == 201, response.text
    assert client.post(f"/api/runs/{run_id}/assessments", json=body).json() == response.json()
    saved = response.json()["current_items"][-1]
    assert saved["target_type"] == "requirement" and saved["result"] == "pass"
    assert saved["option_id"] == body["option_id"] and saved["requirement_id"] == body["requirement_id"]
    assert saved["target_hash"] == body["target_hash"] and saved["reference_ids"] == body["reference_ids"]
    after = snapshot(db_factory, run_id)
    assert after[0] == before[0] + 1 and after[1] == before[1] + 1
    assert after[5:] == before[5:]
