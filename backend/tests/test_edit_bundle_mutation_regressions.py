"""Edit instructions preserve requested work and enforce bounded selections."""

import pytest
from round_robin.visual.feedback import (
    build_edit_bundle,
    regression_targets,
    verify_edit_bundle,
)
from round_robin.visual.schemas import Finding, VisualBrief


def finding(identity, severity="minor"):
    return Finding(id=identity, description=f"Defect {identity}", severity=severity, category="observed_defect").model_dump(mode="json")


def make_bundle(*, findings=(), dispositions=None, intent="Keep the title readable"):
    brief = VisualBrief(purpose="Poster", audience="Visitors", locked_requirements=["Keep the title", "Retain brand mark"]).model_dump(mode="json")
    bundle, bundle_hash = build_edit_bundle(brief=brief, parent_version_id="version_parent", parent_hash="a" * 64,
        findings=list(findings), dispositions={} if dispositions is None else dispositions, intent=intent)
    return brief, bundle, bundle_hash


@pytest.mark.parametrize("intent", ["", " \n ", "x" * 2001, None])
def test_invalid_edit_intents_are_rejected(intent):
    with pytest.raises(ValueError):
        make_bundle(intent=intent)


def test_maximum_length_intent_is_retained():
    value = "x" * 2000
    brief, bundle, bundle_hash = make_bundle(intent=value)
    assert bundle["intent"] == value
    assert verify_edit_bundle(bundle, bundle_hash, brief=brief, parent_version_id="version_parent", parent_hash="a" * 64) == bundle


def test_locked_requirements_and_unselected_blockers_reach_verified_bundle():
    rows = [finding("finding_selected", "critical"), finding("finding_unselected", "material")]
    brief, bundle, bundle_hash = make_bundle(findings=rows, dispositions={"finding_selected": "address"})
    assert bundle["preserve_requirements"] == brief["locked_requirements"]
    assert bundle["regression_targets"] == regression_targets(brief, rows)
    assert {row["id"] for row in bundle["regression_targets"] if row["kind"] == "prior_finding"} == {"finding_selected", "finding_unselected"}
    assert bundle["selected_findings"][0]["decision"] == "address"
    assert verify_edit_bundle(bundle, bundle_hash, brief=brief, parent_version_id="version_parent", parent_hash="a" * 64) == bundle


def test_selection_limit_and_exact_parent_id_are_independent():
    rows = [finding(f"finding_{i}") for i in range(21)]
    twenty = {row["id"]: "address" for row in rows[:20]}
    _, bundle, _ = make_bundle(findings=rows, dispositions=twenty)
    assert len(bundle["selected_findings"]) == 20
    with pytest.raises(ValueError):
        make_bundle(findings=rows, dispositions={row["id"]: "address" for row in rows})
    with pytest.raises(ValueError):
        make_bundle(findings=rows, dispositions={"finding_unknown": "address"})
    with pytest.raises(ValueError):
        make_bundle(findings=rows, dispositions={rows[0]["id"]: "ignore"})


def test_finding_limit_and_unique_id_are_independent():
    rows = [finding(f"finding_{i}") for i in range(40)]
    make_bundle(findings=rows)
    with pytest.raises(ValueError):
        make_bundle(findings=rows + [finding("finding_overflow")])
    with pytest.raises(ValueError):
        make_bundle(findings=[rows[0], rows[0]])
