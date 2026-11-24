from copy import deepcopy

import pytest
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import (
    BudgetOperation,
    Command,
    DeletionRecord,
    EvidenceRecord,
    ExportRecord,
    Meeting,
    Outcome,
    PreflightRecord,
    Run,
    Upload,
)
from round_robin.database.visual_models import DesignSession
from round_robin.operations.recovery import OperationError, purge_record
from sqlalchemy import select

from backend.tests.test_api import command, create, start

pytest_plugins = ("backend.tests.test_api",)


def payload(run_id, reason="ordinary"):
    return {"run_id": run_id, "report_version": 1, "choice": "deferred", "reason": reason, "success_criterion": "criterion", "observed_outcome": "observed", "revisit_date": None}


@pytest.mark.parametrize("damage", ["run", "design", "outcome", "upload", "preflight", "evidence", "claim", "export", "budget"])
def test_inconsistent_owner_refuses_before_manifest_or_any_record_change(application, client, damage):
    meeting = create(client)
    response = start(client, meeting)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    with application.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        if damage == "run":
            run.owner_id = "foreign_owner"
        elif damage == "design":
            session.add(DesignSession(id="design_foreign", meeting_id=meeting["id"], owner_id="foreign_owner", mode="review_only", frozen_brief={}, state={}))
        elif damage == "outcome":
            session.add(Outcome(id="outcome_foreign", meeting_id=meeting["id"], run_id=run_id, owner_id="foreign_owner", revision=1, payload=payload(run_id)))
        elif damage == "upload":
            session.add(Upload(id="upload_foreign", meeting_id=meeting["id"], owner_id="foreign_owner", status="ready", filename="private.txt", media_type="text/plain", canonical_text="private", byte_size=7))
        elif damage == "preflight":
            session.get(PreflightRecord, run.preflight_id).owner_id = "foreign_owner"
        elif damage == "evidence":
            session.add(EvidenceRecord(id="evidence_foreign", meeting_id=meeting["id"], run_id=run_id, owner_id="foreign_owner", content={"private": "keep"}, sha256="0" * 64))
        elif damage == "claim":
            session.add(ClaimVersion(id="claim_foreign", meeting_id=meeting["id"], run_id=run_id, owner_id="foreign_owner", version=1, content={"private": "keep"}, sha256="0" * 64))
        elif damage == "export":
            session.add(ExportRecord(id="export_foreign", run_id=run_id, owner_id="foreign_owner", report_version=1, format="json", status="pending"))
        elif damage == "budget":
            session.add(BudgetOperation(id="budget_foreign", run_id=run_id, owner_id="foreign_owner", kind="model", status="reserved", reserved_usd="0.42"))
        original = (run.status, run.execution_fence, deepcopy(run.input_snapshot))
    calls = []
    application.state.record_deletion_manifest = lambda *args: calls.append(args)
    request = command()
    result = client.request("DELETE", f"/api/meetings/{meeting['id']}", json=request)
    assert result.status_code == 409 and result.json()["error"]["code"] == "deletion_scope_invalid"
    assert calls == []
    with application.state.session_factory() as session:
        run = session.get(Run, run_id)
        assert (run.status, run.execution_fence, run.input_snapshot) == original
        assert session.get(Meeting, meeting["id"]).deleted_at is None
        assert session.get(DeletionRecord, meeting["id"]) is None
        assert session.get(Command, ("owner_local", request["command_id"])) is None


def test_outcome_reads_hide_foreign_owner_and_foreign_run_but_keep_owned(application, client):
    meeting = create(client)
    one = start(client, meeting).json()["id"]
    with application.state.session_factory.begin() as session:
        session.get(Run, one).status = "cancelled"
    two = start(client, meeting).json()["id"]
    with application.state.session_factory.begin() as session:
        session.get(Run, two).owner_id = "foreign_owner"
        session.add_all([
            Outcome(id="outcome_own", meeting_id=meeting["id"], run_id=one, owner_id="owner_local", revision=1, payload=payload(one, "visible")),
            Outcome(id="outcome_foreign", meeting_id=meeting["id"], run_id=one, owner_id="foreign_owner", revision=2, payload=payload(one, "foreign-owner-secret")),
            Outcome(id="outcome_wrong_run", meeting_id=meeting["id"], run_id=two, owner_id="owner_local", revision=3, payload=payload(two, "foreign-run-secret")),
        ])
    response = client.get(f"/api/meetings/{meeting['id']}/outcomes")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["items"]] == ["outcome_own"]
    assert "foreign-owner-secret" not in response.text and "foreign-run-secret" not in response.text


@pytest.mark.parametrize("damage", ["other_run", "missing_run", "non_object", "missing_field"])
def test_owned_outcome_with_mismatched_or_malformed_payload_is_unavailable(application, client, damage):
    meeting = create(client)
    run_id = start(client, meeting).json()["id"]
    corrupt = payload(run_id, "PRIVATE_OUTCOME_SENTINEL")
    if damage == "other_run":
        corrupt["run_id"] = "run_foreign"
    elif damage == "missing_run":
        corrupt.pop("run_id")
    elif damage == "non_object":
        corrupt = ["PRIVATE_OUTCOME_SENTINEL"]
    else:
        corrupt.pop("success_criterion")
    with application.state.session_factory.begin() as session:
        session.add(Outcome(id="outcome_damaged", meeting_id=meeting["id"], run_id=run_id, owner_id="owner_local", revision=1, payload=corrupt))
    response = client.get(f"/api/meetings/{meeting['id']}/outcomes")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "saved_result_unavailable"
    assert "PRIVATE_OUTCOME_SENTINEL" not in response.text
    with application.state.session_factory() as session:
        assert session.get(Outcome, "outcome_damaged").payload == corrupt


def test_replaying_existing_manifest_fails_closed_on_foreign_run(application, client):
    meeting = create(client)
    run_id = start(client, meeting).json()["id"]
    with application.state.session_factory.begin() as session:
        row = session.get(Run, run_id)
        row.owner_id = "foreign_owner"
        original = (row.status, row.execution_fence, deepcopy(row.input_snapshot))
    with pytest.raises(OperationError), application.state.session_factory.begin() as session:
        purge_record(session, meeting["id"], "owner_local", 1)
    with application.state.session_factory() as session:
        row = session.get(Run, run_id)
        assert (row.status, row.execution_fence, row.input_snapshot) == original
        assert session.get(Meeting, meeting["id"]).deleted_at is None
        assert session.scalar(select(DeletionRecord).where(DeletionRecord.meeting_id == meeting["id"])) is None


def test_normal_deletion_and_manifest_replay_still_complete(application, client):
    meeting = create(client)
    run_id = start(client, meeting).json()["id"]
    response = client.request("DELETE", f"/api/meetings/{meeting['id']}", json=command())
    assert response.status_code == 200, response.text
    with application.state.session_factory.begin() as session:
        purge_record(session, meeting["id"], "owner_local", 1)
    with application.state.session_factory() as session:
        assert session.get(Run, run_id).status == "cancelled"
        assert session.get(DeletionRecord, meeting["id"]).status == "purged"
