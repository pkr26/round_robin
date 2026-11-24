"""Explicit owner attestation of exact saved inputs; never source-wide verification."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from round_robin.database.models import EvidenceRecord, ReportVersion, Run, Step, utcnow
from round_robin.database.services import append_event, new_id
from round_robin.orchestration.reporting import build_report
from round_robin.reports.read_adapters import read_runtime_report, read_stored_evidence
from round_robin.research.claims import ClaimRejected
from round_robin.research.discussion import assessment_target, current_assessments
from round_robin.research.reuse import claim_packet
from round_robin.schemas.discussion import AssessmentPage, ScopedAssessment, ScopedAssessmentCommand
from .auth import get_db, require_owner
from .commands import command_begin, command_commit, fail
from .events import owned_run, latest_report

router = APIRouter()


def _evidence(session, run):
    return {row.id: read_stored_evidence(row, run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id)
        for row in session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id))}


def assessment_view(session, run):
    evidence = _evidence(session, run)
    brief = run.configuration.get("resolved_brief", run.input_snapshot)
    targets = []
    for option in brief.get("options", []):
        refs = sorted({item["source_ref"] for item in option.get("cost_components", [])} & set(evidence))
        targets.append({"target_type": "option_costs", "option_id": option["id"], "requirement_id": None,
            "target_hash": assessment_target(run, "option_costs", option["id"]), "label": f"Complete cost inputs: {option['label']}", "reference_ids": refs})
        for requirement in brief.get("hard_requirements", []):
            if requirement["metric"] != "total_cost":
                targets.append({"target_type": "requirement", "option_id": option["id"], "requirement_id": requirement["id"],
                    "target_hash": assessment_target(run, "requirement", option["id"], requirement["id"]),
                    "label": f"{option['label']}: {requirement['description']}", "reference_ids": sorted(evidence)})
    return {"items": deepcopy(run.configuration.get("evidence_assessments", [])), "current_items": current_assessments(run, evidence),
        "targets": targets, "run_version": run.version, "scope_notice": "Owner attestations bind exact inputs and cited passages. They do not establish independent supplier verification or model accuracy."}


@router.get("/api/runs/{run_id}/assessments", response_model=AssessmentPage)
def read_assessments(run_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner)
    return assessment_view(session, run)


@router.post("/api/runs/{run_id}/assessments", status_code=201, response_model=AssessmentPage)
def attest_inputs(run_id: str, body: ScopedAssessmentCommand, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "attest_inputs", {"run_id": run_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        run = owned_run(session, run_id, owner, lock=True)
        if run.version != body.expected_run_version:
            fail("run_version_conflict", "Reopen the current run before recording an attestation")
        if run.status not in {"completed", "completed_with_limitations", "failed", "cancelled"}:
            fail("assessment_run_active", "Wait until discussion stops before attesting its exact saved inputs")
        from .events import ledger_unavailable
        if run.configuration.get("saved_state_unavailable") is True:
            fail("saved_state_integrity_failed", "Saved state could not be verified; no new assessment report can be created.", 503, "read")
        if ledger_unavailable(session, run):
            fail("ledger_integrity_failed", "Saved accounting records could not be verified; no new assessment report can be created.", 503, "read")
        try:
            digest = assessment_target(run, body.target_type, body.option_id, body.requirement_id)
        except ClaimRejected as error:
            fail("assessment_scope_invalid", str(error))
        if digest != body.target_hash:
            fail("assessment_target_changed", "The exact option or requirement inputs changed")
        evidence = _evidence(session, run)
        if len(set(body.reference_ids)) != len(body.reference_ids) or not set(body.reference_ids) <= set(evidence):
            fail("assessment_reference_invalid", "Use unique evidence identities from this exact run")
        brief = run.configuration.get("resolved_brief", run.input_snapshot)
        today = utcnow().astimezone(ZoneInfo(brief.get("timezone", "America/Phoenix"))).date()
        if body.valid_until is not None and body.valid_until < today:
            fail("assessment_expired", "An expired attestation cannot establish current support")
        if body.target_type == "option_costs":
            option = next(item for item in brief["options"] if item["id"] == body.option_id)
            required = {item["source_ref"] for item in option.get("cost_components", [])}
            if body.result != "supported" or not required or not required <= set(body.reference_ids):
                fail("assessment_cost_incomplete", "Attest all exact cost components and cite every supplied cost source")
            if any(item.get("quote_valid_until") and item["quote_valid_until"] < today.isoformat() for item in option["cost_components"]):
                fail("assessment_quote_expired", "Create a follow-up with current quotes before attesting costs")
        elif body.result == "supported":
            fail("assessment_result_invalid", "A requirement needs pass, fail, unknown or not applicable")
        records = deepcopy(run.configuration.get("evidence_assessments", []))
        if len(records) >= 300:
            fail("assessment_limit", "This run reached its immutable assessment limit; use a linked follow-up")
        previous = next((item for item in reversed(records) if (item["target_type"], item["option_id"], item.get("requirement_id")) == (body.target_type, body.option_id, body.requirement_id)), None)
        value = ScopedAssessment.model_validate({**body.model_dump(mode="json", exclude={"command_id", "expected_run_version"}),
            "id": new_id("assessment"), "evidence_hashes": {ref: evidence[ref]["sha256"] for ref in body.reference_ids},
            "actor_id": owner, "run_id": run.id, "recorded_at": utcnow(), "supersedes_id": previous["id"] if previous else None}).model_dump(mode="json")
        records.append(value)
        run.configuration = {**run.configuration, "evidence_assessments": records, "assessment_policy_version": "scoped_user_attestation_v1"}
        append_event(session, run, "evidence.attestation_recorded", {"assessment": value})
        previous_report = latest_report(session, run)
        if previous_report:
            old = read_runtime_report(session, previous_report)
            steps = list(session.scalars(select(Step).where(Step.run_id == run.id).order_by(Step.created_at, Step.id)))
            parent = session.get(Run, run.parent_run_id) if run.parent_run_id else None
            parent_report = latest_report(session, parent) if parent and (parent.owner_id, parent.meeting_id) == (run.owner_id, run.meeting_id) else None
            report_id, version = new_id("report"), run.report_version + 1
            report = build_report(run, steps, status=run.status, stop_reason=run.stop_reason or "scoped_attestation", report_id=report_id,
                version=version, usage=old["usage"], evidence=list(evidence.values()), claims=claim_packet(session, run),
                parent_report=parent_report.content if parent_report else None, parent_brief=parent.input_snapshot if parent_report else None)
            session.add(ReportVersion(id=report_id, run_id=run.id, version=version, event_cutoff=run.event_sequence + 1, content=report))
            run.report_version = version
            append_event(session, run, "report.reassessed", {"report_id": report_id, "version": version, "assessment_id": value["id"], "inference_requests": 0})
        run.version += 1
        result = assessment_view(session, run)
        result["report_version"] = run.report_version
        command_commit(session, request, owner, body.command_id, "attest_inputs", result, resource_id=run.id, meeting_id=run.meeting_id, status_code=201)
        return result
