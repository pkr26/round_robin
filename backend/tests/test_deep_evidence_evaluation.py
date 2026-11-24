"""Adversarial prospective study records; these are bookkeeping fixtures only."""

from dataclasses import replace

import pytest

from backend.tests.test_evaluation import study
from backend.tests.test_release_evaluation import manifest as release_manifest, result as release_result
from backend.tests.test_visual_evaluation import manifest as visual_manifest, output as visual_output
from experiments.evaluation import build_blinded_packets, load_dataset, summarize_workflow, validate_dataset
from experiments.release import summarize as release_summary, validate_manifest
from experiments.visual_evaluation import build_plan, summarize as visual_summary, validate_manifest as visual_validate


@pytest.mark.parametrize("missing", ["recorded_at", "grade_output_hash"])
def test_release_cannot_invent_missing_output_time_or_grade_binding(missing):
    manifest = release_manifest()
    record = release_result(manifest["tasks"][0], validate_manifest(manifest))
    record["recorded_at"] = "2026-11-16T12:30:00+00:00"
    record["original_grades"][0]["output_hash"] = record["output_hash"]
    if missing == "recorded_at":
        record.pop("recorded_at")
    else:
        record["original_grades"][0].pop("output_hash")
    with pytest.raises(ValueError):
        release_summary(manifest, [record])


@pytest.mark.parametrize("change", [{"task_version": True}, {"hard_constraint_findings": ("",)}, {"passage_support_findings": "Not a finding array"}, {"reviewer_id": 1}])
def test_manual_grade_requires_typed_attributable_nonblank_findings(change):
    _, _, grades, _ = study()
    with pytest.raises(ValueError):
        replace(grades[0], **change)


def test_fixed_evidence_adjudication_cannot_predate_the_original_reviews():
    manifest, outputs, grades, mapping = study()
    primary = next(grade for grade in grades if grade.task_id == "BUD-01" and mapping["identities"][grade.packet_token]["primary"])
    originals = [grade for grade in grades if grade.packet_token == primary.packet_token]
    adjudicated = replace(primary, grade_id="adjudication_early", reviewer_id="adjudicator", timestamp="2026-11-15T09:00:00-07:00", adjudication_of=tuple(grade.grade_id for grade in originals), adjudication_reason="A reason cannot make a prospective review occur earlier.")
    with pytest.raises(ValueError):
        summarize_workflow(manifest, "independent_review", outputs, [*grades, adjudicated], mapping)


def test_failed_repeat_cannot_claim_complete_successful_review_coverage():
    manifest, outputs, grades, mapping = study()
    failed = next(output for output in outputs if ".r1" in output.trial_id)
    changed = [replace(output, status="failed") if output.trial_id == failed.trial_id else output for output in outputs]
    _, remapped = build_blinded_packets(manifest, changed, seed="review-plan-v1")
    summary = summarize_workflow(manifest, "independent_review", changed, grades, remapped)
    assert not summary["repeat_review_complete"] and not summary["review_complete"]
    assert summary["repeat_outputs_retained"] == 10


def test_visual_summary_rejects_nonfinite_accumulation_of_finite_active_observations():
    manifest = visual_manifest()
    trials = [trial for trial in build_plan(manifest) if trial["arm"] == "generation_only"][:2]
    records = [visual_output(trial) for trial in trials]
    for record in records:
        record["usage"]["active_seconds"] = 1e308
    with pytest.raises(ValueError):
        visual_summary(manifest, records)


@pytest.mark.parametrize("kind", ["fixed", "release", "visual"])
def test_study_task_identities_require_nonblank_strings(kind):
    if kind == "fixed":
        manifest = load_dataset()
        original_id = manifest["tasks"][0]["task_id"]
        manifest["tasks"][0]["task_id"] = True
        for field in ("repeated_task_ids", "double_review_task_ids"):
            manifest[field] = [True if identity == original_id else identity for identity in manifest[field]]
        validator = validate_dataset
    elif kind == "release":
        manifest = release_manifest()
        manifest["tasks"][0]["task_id"] = True
        validator = validate_manifest
    else:
        manifest = visual_manifest()
        manifest["briefs"][0]["brief_id"] = True
        manifest["repeated_brief_ids"][0] = True
        manifest["double_review_brief_ids"][0] = True
        validator = visual_validate
    with pytest.raises(ValueError):
        validator(manifest)


def test_original_grade_copies_mutable_findings_before_retaining_them():
    _, _, grades, _ = study()
    findings = ["Original independent finding"]
    grade = replace(grades[0], hard_constraint_findings=findings)
    findings[0] = "A favorable replacement"
    assert grade.hard_constraint_findings == ("Original independent finding",)
