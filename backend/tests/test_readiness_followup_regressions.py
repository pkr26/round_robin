import pytest
from round_robin.fixtures import sample_brief, sample_report
from round_robin.reports.readiness import derive_readiness


@pytest.fixture
def report():
    return sample_report(sample_brief(), scenario_id="base12")


def derive(report, *, comparison=None, dependencies=None, coverage=None):
    return derive_readiness(selected_option_id=report.recommendation.option_id,
        comparison=report.comparison if comparison is None else comparison,
        dependencies=report.essential_dependencies if dependencies is None else dependencies,
        coverage=report.assessment if coverage is None else coverage,
        required_requirement_ids=set(report.hard_requirement_ids))


def test_selected_failed_requirement_is_insufficient_even_if_another_option_passes(report):
    comparison = list(report.comparison)
    selected = comparison[0]
    results = list(selected.constraint_results)
    results[0] = results[0].model_copy(update={"result": "fail"})
    comparison[0] = selected.model_copy(update={"constraint_results": results})
    assert derive(report, comparison=comparison)[0] == "insufficient_evidence"


@pytest.mark.parametrize("providers", [["openai", "anthropic"], ["openai", "anthropic", "anthropic"]])
def test_exactly_two_distinct_proposals_meet_minimum_review_coverage(report, providers):
    coverage = report.assessment.model_copy(update={"proposal_providers": providers})
    assert derive(report, coverage=coverage)[0] == "ready_to_choose"


@pytest.mark.parametrize("disputed", [False, True])
def test_selected_unknown_requirement_is_never_ready(report, disputed):
    comparison = list(report.comparison)
    selected = comparison[0]
    results = list(selected.constraint_results)
    results[0] = results[0].model_copy(update={"result": "unknown", "disputed": disputed})
    comparison[0] = selected.model_copy(update={"constraint_results": results})
    assert derive(report, comparison=comparison)[0] == "conditional"


def test_selected_required_pass_without_basis_is_not_ready(report):
    comparison = list(report.comparison)
    selected = comparison[0]
    results = list(selected.constraint_results)
    results[0] = results[0].model_copy(update={"basis_refs": []})
    comparison[0] = selected.model_copy(update={"constraint_results": results})
    assert derive(report, comparison=comparison)[0] == "conditional"


@pytest.mark.parametrize("status", ["assumed", "unverified", "mixed", "contradicted"])
def test_non_supported_dependency_never_establishes_readiness_even_if_marked_accepted(report, status):
    dependencies = list(report.essential_dependencies)
    dependencies[0] = dependencies[0].model_copy(update={"status": status, "accepted_scoped_support": True})
    assert derive(report, dependencies=dependencies)[0] == "conditional"
