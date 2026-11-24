from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.tests.test_orchestration import drain, factory, seed
from round_robin.database.models import ReportVersion
from round_robin.fixtures import sample_bundle
from round_robin.schemas.runtime import RuntimeDecisionReport


@pytest.fixture
def runtime_body(factory):
    run_id = seed(factory)
    drain(factory)
    with factory() as session:
        return deepcopy(session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content)


def test_runtime_report_validates_saved_scope_and_preserves_sample_contract(runtime_body):
    report = RuntimeDecisionReport.model_validate(runtime_body, context={"run_id": runtime_body["run_id"], "allowed_evidence_ids": runtime_body["evidence_ids"], "allowed_contribution_ids": runtime_body["contribution_ids"]})
    assert report.simulated and report.usage.request_count == 6
    assert sample_bundle().fictional
    assert sample_bundle().scenarios[0].report.readiness == "ready_to_choose"


@pytest.mark.parametrize("mutation", ["missing_audit", "negative_audit", "critical_defect", "missing_requirement", "simulated_ready"])
def test_runtime_cannot_publish_ready_from_missing_or_failed_review(runtime_body, mutation):
    body = deepcopy(runtime_body)
    body["readiness"] = "ready_to_choose"
    body["readiness_unavailable_reason"] = None
    body["recommendation"] = {"option_id": body["comparison"][0]["option_id"], "summary": "An improperly upgraded recommendation", "conditions": []}
    if mutation == "missing_audit":
        body["assessment"]["audit_succeeded"] = False
        body["assessment"]["auditor"] = None
    elif mutation == "negative_audit":
        body["assessment"]["audit_succeeded"] = False
    elif mutation == "critical_defect":
        body["assessment"]["unresolved_critical_defect"] = True
    elif mutation == "missing_requirement":
        body["comparison"][0]["constraint_results"] = []
    with pytest.raises(ValidationError):
        RuntimeDecisionReport.model_validate(body)


def test_runtime_rejects_foreign_calculation_and_dependency_references(runtime_body):
    body = deepcopy(runtime_body)
    body["calculations"][0]["inputs"][0]["source_ref"] = "evidence_other_owner_run"
    with pytest.raises(ValidationError, match="outside this run"):
        RuntimeDecisionReport.model_validate(body)
    body = deepcopy(runtime_body)
    body["essential_dependencies"][0]["reference_ids"] = ["evidence_other_owner_run"]
    with pytest.raises(ValidationError, match="within this report"):
        RuntimeDecisionReport.model_validate(body)
    body = deepcopy(runtime_body)
    body["evidence_ids"].append("evidence_forged_scope")
    with pytest.raises(ValidationError, match="scope must match"):
        RuntimeDecisionReport.model_validate(body, context={"allowed_evidence_ids": runtime_body["evidence_ids"]})


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed_with_limitations"])
def test_empty_partial_report_has_explicit_unavailable_assessment(runtime_body, status):
    body = deepcopy(runtime_body)
    body.update(execution_status=status, comparison=[], calculations=[], recommendation=None, readiness=None, readiness_reasons=["No useful synthesis survived."], readiness_unavailable_reason="No useful synthesis survived.", essential_dependencies=[], next_evidence=[])
    body["assessment"].update(synthesis_valid=False, audit_succeeded=False, synthesizer=None, auditor=None)
    assert RuntimeDecisionReport.model_validate(body).readiness is None
    body["execution_status"] = "completed"
    with pytest.raises(ValidationError, match="empty comparison"):
        RuntimeDecisionReport.model_validate(body)


def test_runtime_unknown_usage_never_becomes_zero_or_duplicate_exposure(runtime_body):
    body = deepcopy(runtime_body)
    body["usage"].update(status="unknown", actual_cost="0", unknown_operation_ids=["attempt_uncertain"])
    with pytest.raises(ValidationError, match="Unknown usage"):
        RuntimeDecisionReport.model_validate(body)
    body["usage"].update(actual_cost=None, unknown_operation_ids=["attempt_uncertain", "attempt_uncertain"])
    with pytest.raises(ValidationError, match="counted once"):
        RuntimeDecisionReport.model_validate(body)


def test_runtime_comparison_cannot_disagree_with_checked_calculation(runtime_body):
    body = deepcopy(runtime_body)
    body["comparison"][0]["total"] = "999.00"
    body["comparison"][0]["missing_components"] = []
    with pytest.raises(ValidationError, match="total must match"):
        RuntimeDecisionReport.model_validate(body)


def test_runtime_scope_context_cannot_hide_an_option_or_essential_requirement(runtime_body):
    body = deepcopy(runtime_body)
    body["comparison"] = body["comparison"][:-1]
    with pytest.raises(ValidationError, match="every frozen considered option"):
        RuntimeDecisionReport.model_validate(body, context={"allowed_option_ids": [item["option_id"] for item in runtime_body["comparison"]]})
    body = deepcopy(runtime_body)
    body["hard_requirement_ids"] = body["hard_requirement_ids"][:-1]
    with pytest.raises(ValidationError, match="requirements differ"):
        RuntimeDecisionReport.model_validate(body, context={"hard_requirement_ids": runtime_body["hard_requirement_ids"]})
