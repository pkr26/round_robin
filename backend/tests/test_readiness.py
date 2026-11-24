import pytest
from pydantic import ValidationError

from round_robin.fixtures import sample_brief, sample_report
from round_robin.reports.readiness import derive_readiness
from round_robin.schemas.reports import DecisionReport


def assessed(report, **changes):
    return derive_readiness(selected_option_id=report.recommendation.option_id, comparison=changes.get("comparison", report.comparison), dependencies=changes.get("dependencies", report.essential_dependencies), coverage=changes.get("coverage", report.assessment), required_requirement_ids=set(report.hard_requirement_ids))


def test_complete_fixture_has_ready_assessment_separate_from_status():
    report = sample_report(sample_brief(), scenario_id="base12")
    assert report.readiness == "ready_to_choose"
    assert report.execution_status == "completed"
    assert report.usage.request_count == 0
    assert report.usage.simulated is True


def test_missing_cost_keeps_completed_report_conditional():
    report = sample_report(sample_brief(24, True), scenario_id="missing_installation")
    assert report.execution_status == "completed"
    assert report.readiness == "conditional"
    assert report.comparison[1].total is None
    assert report.comparison[1].constraint_results[0].result == "unknown"
    assert "cheapest all-in choice is not established" in report.recommendation.summary


def test_missing_synthesis_does_not_fabricate_recommendation_or_readiness():
    report = sample_report(sample_brief(), scenario_id="base12", partial=True)
    assert report.recommendation is None
    assert report.readiness is None
    assert report.readiness_unavailable_reason
    assert report.execution_status == "completed_with_limitations"
    assert report.comparison[0].total == "1600.00"


@pytest.mark.parametrize("readiness", ["conditional", "no_feasible_option", "insufficient_evidence", "ready_to_choose"])
def test_schema_rejects_any_published_assessment_without_valid_synthesis(readiness):
    report = sample_report(sample_brief(), scenario_id="base12", partial=True)
    data = report.model_dump(mode="json")
    data.update(readiness=readiness, readiness_unavailable_reason=None)
    with pytest.raises(ValidationError, match="Missing synthesis"):
        DecisionReport.model_validate(data)


def test_schema_rejects_recommendation_with_unavailable_assessment():
    report = sample_report(sample_brief(), scenario_id="base12", partial=True)
    data = report.model_dump(mode="json")
    data["recommendation"] = {"option_id": "option_a", "summary": "An invented recommendation.", "conditions": []}
    with pytest.raises(ValidationError, match="Missing synthesis"):
        DecisionReport.model_validate(data)


@pytest.mark.parametrize("changes", [{"auditor": "openai"}, {"auditor": None}, {"audit_succeeded": False}, {"unresolved_critical_defect": True}, {"proposal_providers": ["openai"]}, {"synthesizer": None}])
def test_same_provider_or_incomplete_review_never_publishes_ready(changes):
    report = sample_report(sample_brief(), scenario_id="base12")
    coverage = report.assessment.model_copy(update=changes)
    readiness, reasons = assessed(report, coverage=coverage)
    assert readiness == "conditional"
    assert reasons
    with pytest.raises(ValidationError):
        DecisionReport.model_validate({**report.model_dump(), "assessment": coverage.model_dump()})


def test_unknown_essential_dependency_remains_conditional():
    report = sample_report(sample_brief(), scenario_id="base12")
    dependencies = [item.model_copy(update={"status": "unverified", "accepted_scoped_support": False}) for item in report.essential_dependencies]
    assert assessed(report, dependencies=dependencies)[0] == "conditional"


def test_missing_hard_requirement_assessment_is_not_a_pass():
    report = sample_report(sample_brief(), scenario_id="base12")
    comparison = [report.comparison[0].model_copy(update={"constraint_results": []}), *report.comparison[1:]]
    assert assessed(report, comparison=comparison)[0] == "conditional"


def test_requirement_missing_from_every_option_cannot_disappear():
    report = sample_report(sample_brief(), scenario_id="base12")
    comparison = [option.model_copy(update={"constraint_results": option.constraint_results[1:]}) for option in report.comparison]
    assert assessed(report, comparison=comparison)[0] == "conditional"


def test_supported_dependency_without_a_reference_cannot_establish_ready():
    report = sample_report(sample_brief(), scenario_id="base12")
    dependencies = [item.model_copy(update={"reference_ids": []}) for item in report.essential_dependencies]
    assert assessed(report, dependencies=dependencies)[0] == "conditional"


def test_unsupported_failures_do_not_establish_no_feasible():
    report = sample_report(sample_brief(), scenario_id="base12")
    comparison = [item.model_copy(update={"constraint_results": [result.model_copy(update={"result": "fail", "basis_refs": []}) for result in item.constraint_results]}) for item in report.comparison]
    readiness, _ = derive_readiness(selected_option_id=None, comparison=comparison, dependencies=[], coverage=report.assessment, required_requirement_ids=set(report.hard_requirement_ids))
    assert readiness == "insufficient_evidence"


def test_unknowns_cannot_establish_no_feasible_option():
    report = sample_report(sample_brief(), scenario_id="base12")
    comparison = [item.model_copy(update={"constraint_results": [result.model_copy(update={"result": "unknown"}) for result in item.constraint_results]}) for item in report.comparison]
    readiness, _ = derive_readiness(selected_option_id=None, comparison=comparison, dependencies=[], coverage=report.assessment, required_requirement_ids=set(report.hard_requirement_ids))
    assert readiness == "insufficient_evidence"


def test_every_option_requires_demonstrated_disqualification_for_no_feasible():
    report = sample_report(sample_brief(), scenario_id="base12")
    comparison = [item.model_copy(update={"constraint_results": [result.model_copy(update={"result": "fail"}) for result in item.constraint_results]}) for item in report.comparison]
    readiness, _ = derive_readiness(selected_option_id=None, comparison=comparison, dependencies=[], coverage=report.assessment, required_requirement_ids=set(report.hard_requirement_ids))
    assert readiness == "no_feasible_option"
