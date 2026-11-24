"""Owner attestations bind to durable, exact comparison scope."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from round_robin.research.claims import ClaimRejected
from round_robin.research.discussion import assessment_target, current_assessments
from round_robin.schemas.discussion import ScopedAssessment


@pytest.fixture
def brief():
    return {
        "options": [
            {"id": "option_alpha", "label": "Alpha", "cost_components": [{"amount": "10.00", "source_ref": "evidence_one"}]},
            {"id": "option_beta", "label": "Beta", "cost_components": []},
        ],
        "currency": "USD", "horizon_months": 12, "timezone": "America/Phoenix",
        "hard_requirements": [
            {"id": "requirement_manual", "metric": "manual", "description": "Fits the available space"},
            {"id": "requirement_cost", "metric": "total_cost", "description": "Within budget"},
        ],
    }


def make_run(brief, *, resolved=None):
    configuration = {} if resolved is None else {"resolved_brief": resolved}
    return SimpleNamespace(id="run_one", owner_id="owner_one", input_snapshot=brief, configuration=configuration)


def test_existing_assessment_scope_hashes_remain_compatible(brief):
    run = make_run(brief)
    # Existing saved attestations contain these hashes; changing the scope
    # serialization would silently invalidate those historical records.
    assert assessment_target(run, "option_costs", "option_alpha") == "1ba9193d927e4a4fe5a1e3afc9549d25a8d4800b6b983ddc47d8811bfbcf1441"
    assert assessment_target(run, "requirement", "option_alpha", "requirement_manual") == "17847357cbffc1f2bfae6dd5c5cfd35b237f62c9f81f89286b1eece90abc0eb2"


@pytest.mark.parametrize("field,value", [("currency", "EUR"), ("horizon_months", 24), ("timezone", "UTC")])
def test_cost_attestation_changes_when_comparison_context_changes(brief, field, value):
    original = assessment_target(make_run(brief), "option_costs", "option_alpha")
    changed = {**brief, field: value}
    assert assessment_target(make_run(changed), "option_costs", "option_alpha") != original


def test_assessment_binds_the_selected_option_and_requirement(brief):
    run = make_run(brief)
    assert assessment_target(run, "option_costs", "option_alpha") != assessment_target(run, "option_costs", "option_beta")
    original = assessment_target(run, "requirement", "option_alpha", "requirement_manual")
    changed = deepcopy(brief)
    changed["hard_requirements"][0]["description"] = "Fits a revised space"
    assert assessment_target(make_run(changed), "requirement", "option_alpha", "requirement_manual") != original
    unchanged = deepcopy(brief)
    unchanged["options"][1]["label"] = "Unrelated option update"
    assert assessment_target(make_run(unchanged), "requirement", "option_alpha", "requirement_manual") == original


def test_resolved_brief_takes_precedence_and_snapshot_is_a_fallback(brief):
    baseline = assessment_target(make_run(brief), "option_costs", "option_alpha")
    resolved = {**brief, "currency": "EUR"}
    assert assessment_target(make_run(brief, resolved=resolved), "option_costs", "option_alpha") != baseline
    assert assessment_target(make_run(resolved), "option_costs", "option_alpha") == assessment_target(make_run(brief, resolved=resolved), "option_costs", "option_alpha")


@pytest.mark.parametrize(("target_type", "option_id", "requirement_id"), [
    ("option_costs", "option_unknown", None),
    ("option_costs", "option_alpha", "requirement_manual"),
    ("requirement", "option_alpha", "requirement_unknown"),
    ("requirement", "option_alpha", "requirement_cost"),
    ("unknown", "option_alpha", None),
])
def test_invalid_or_cross_scope_assessment_targets_are_rejected(brief, target_type, option_id, requirement_id):
    with pytest.raises(ClaimRejected):
        assessment_target(make_run(brief), target_type, option_id, requirement_id)


def test_missing_option_or_requirement_collections_are_controlled_rejections(brief):
    with pytest.raises(ClaimRejected):
        assessment_target(make_run({**brief, "options": []}), "option_costs", "option_alpha")
    missing_options = {key: value for key, value in brief.items() if key != "options"}
    with pytest.raises(ClaimRejected):
        assessment_target(make_run(missing_options), "option_costs", "option_alpha")
    missing_requirements = {key: value for key, value in brief.items() if key != "hard_requirements"}
    with pytest.raises(ClaimRejected):
        assessment_target(make_run(missing_requirements), "requirement", "option_alpha", "requirement_manual")


def test_saved_attestation_is_dropped_when_scope_or_evidence_changes(brief):
    run = make_run(brief, resolved=deepcopy(brief))
    digest = "a" * 64
    record = ScopedAssessment(
        id="assessment_one", target_type="option_costs", option_id="option_alpha",
        target_hash=assessment_target(run, "option_costs", "option_alpha"),
        reference_ids=["evidence_one"], evidence_hashes={"evidence_one": digest},
        result="supported", attestation="I attest the exact supplied cost inputs.",
        actor_id=run.owner_id, run_id=run.id, recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    ).model_dump(mode="json")
    run.configuration["evidence_assessments"] = [record]
    evidence = {"evidence_one": {"sha256": digest}}
    now = datetime(2026, 1, 2, tzinfo=UTC)
    assert current_assessments(run, evidence, now=now) == [record]
    assert current_assessments(run, {"evidence_one": {"sha256": "b" * 64}}, now=now) == []
    run.configuration["resolved_brief"]["currency"] = "EUR"
    assert current_assessments(run, evidence, now=now) == []
