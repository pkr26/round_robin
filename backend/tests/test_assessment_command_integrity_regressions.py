"""Owner attestation commands must stay scoped and atomic without inference."""
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
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
from round_robin.reports.exports import render_saved_report
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report

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


def test_requirement_can_be_reassessed_with_a_current_source_after_a_quote_expires(attestation):
    db_factory, run_id, client = attestation
    with db_factory.begin() as session:
        run = session.get(Run, run_id)
        brief = deepcopy(run.configuration["resolved_brief"])
        line = brief["options"][0]["cost_components"][0]
        line["quote_valid_until"] = (utcnow().date() - timedelta(days=2)).isoformat()
        expired_ref = line["source_ref"]
        run.configuration = {**run.configuration, "resolved_brief": brief}
    body = payload(client, run_id, "requirement")
    assert expired_ref not in body["reference_ids"]
    invalid = {**body, "command_id": str(uuid4()), "reference_ids": [expired_ref]}
    path = f"/api/runs/{run_id}/assessments"
    assert client.post(path, json=invalid).json()["error"]["code"] == "assessment_quote_expired"
    response = client.post(path, json={**body, "reference_ids": body["reference_ids"][:1]})
    assert response.status_code == 201, response.text
    assert response.json()["current_items"][0]["result"] == "pass"
    report = client.get(f"/api/runs/{run_id}/report").json()
    assert report["version"] == 2 and report["recommendation"] is None


def test_only_selected_passages_propagate_to_receipt_requirement_and_exports(attestation):
    db_factory, run_id, client = attestation
    path = f"/api/runs/{run_id}"
    body = payload(client, run_id, "requirement")
    all_refs = body["reference_ids"]
    assert len(all_refs) > 2
    selected = [all_refs[-1], all_refs[0]]
    remaining = set(all_refs) - set(selected)
    sources = {item["id"]: item for item in client.get(path + "/snapshot").json()["evidence"]}
    hashes = {ref: sources[ref]["sha256"] for ref in selected}
    assert all(sources[ref]["text"] and sources[ref]["source_title"] and sources[ref]["verified_at"] for ref in selected)
    response = client.post(path + "/assessments", json={**body, "reference_ids": selected})
    assert response.status_code == 201, response.text
    receipt = response.json()["current_items"][0]
    assert receipt["reference_ids"] == selected and receipt["evidence_hashes"] == hashes
    report = client.get(path + "/report").json()
    assert report["evidence_assessments"] == [receipt]
    option = next(item for item in report["comparison"] if item["option_id"] == body["option_id"])
    verdict = next(item for item in option["constraint_results"] if item["requirement_id"] == body["requirement_id"])
    assert verdict["method"] == "human" and verdict["basis_refs"] == selected
    for format in ("markdown", "pdf"):
        content, _ = render_saved_report(report, format, title="Selected evidence", sources=list(sources.values()))
        text = content.decode().replace("\\", "") if format == "markdown" else "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
        section = text.split("Cited passages", 1)[1].split("Exact supplied inputs", 1)[0]
        assert all(ref in section and digest in "".join(section.split()) for ref, digest in hashes.items())
        assert not any(ref in section for ref in remaining)
    # A rewritten report cannot attribute a different, otherwise eligible
    # run-scoped citation to the owner's original command.
    with db_factory.begin() as session:
        row = session.get(ReportVersion, report["id"])
        forged = deepcopy(row.content)
        forged_receipt = forged["evidence_assessments"][0]
        forged_receipt["reference_ids"] = [all_refs[1]]
        forged_receipt["evidence_hashes"] = {all_refs[1]: sources[all_refs[1]]["sha256"]}
        for item in forged["comparison"]:
            for result in item["constraint_results"]:
                if result["method"] == "human":
                    result["basis_refs"] = [all_refs[1]]
        row.content = forged
        with pytest.raises(UnreadableReport):
            read_runtime_report(session, row)


def test_cost_attestation_accepts_corroborating_passages_only_with_all_required_sources(attestation):
    _, run_id, client = attestation
    cost = payload(client, run_id)
    other = next(ref for ref in payload(client, run_id, "requirement")["reference_ids"] if ref not in cost["reference_ids"])
    path = f"/api/runs/{run_id}/assessments"
    incomplete = {**cost, "command_id": str(uuid4()), "reference_ids": [*cost["reference_ids"][1:], other]}
    response = client.post(path, json=incomplete)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "assessment_cost_incomplete"
    cost["reference_ids"].append(other)
    response = client.post(path, json=cost)
    assert response.status_code == 201, response.text
    assert response.json()["current_items"][0]["reference_ids"] == cost["reference_ids"]
