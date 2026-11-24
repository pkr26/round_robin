from copy import deepcopy

import pytest

from experiments.release import STRATA, summarize, validate_manifest


def manifest():
    return {"mode": "held_out_end_to_end", "schema_version": "1.0", "frozen_at": "2026-11-16T12:00:00+00:00", "protocol_decision_id": "synthetic_unit_test_only", "configuration_fingerprint": "synthetic_configuration", "tasks": [{"task_id": f"synthetic_{stratum}_{index}", "stratum": stratum, "brief": f"Distinct synthetic unit-test task {stratum} {index}", "hard_requirements": ["Synthetic"], "critical_dependencies": ["Synthetic"], "acceptable_outcomes": ["Synthetic"], "evidence_manifest": ["Synthetic"]} for stratum in STRATA for index in range(8)]}


def result(task, digest, *, score=4, status="completed", critical=False):
    return {"task_id": task["task_id"], "dataset_fingerprint": digest, "configuration_fingerprint": "synthetic_configuration", "status": status, "recorded_at": "2026-11-16T12:30:00+00:00", "output_hash": "synthetic_hash", "original_grades": [{"grade_id": "grade_" + task["task_id"], "reviewer_id": "synthetic_human_fixture", "timestamp": "2026-11-16T13:00:00+00:00", "output_hash": "synthetic_hash", "usefulness": score, "critical_defect": critical, "critical_defect_reason": "Synthetic hard constraint defect" if critical else None}]}


def test_missing_results_stay_in_48_denominator_and_no_automatic_release():
    study = manifest(); digest = validate_manifest(study)
    summary = summarize(study, [result(task, digest) for task in study["tasks"][:38]])
    assert not summary["numerical_usefulness_target"] and len(summary["missing_primary_task_ids"]) == 10
    assert summary["independent_primary_tasks"] == 48 and not summary["phase_5_passed"]


def test_39_primary_results_meet_only_numerical_target():
    study = manifest(); digest = validate_manifest(study)
    summary = summarize(study, [result(task, digest) for task in study["tasks"][:39]])
    assert summary["numerical_usefulness_target"] and not summary["phase_5_passed"]


def test_repeats_and_failed_outputs_cannot_replace_primary():
    study = manifest(); digest = validate_manifest(study)
    first = result(study["tasks"][0], digest)
    with pytest.raises(ValueError): summarize(study, [first, deepcopy(first)])
    with pytest.raises(ValueError): summarize(study, [{**first, "repeat_index": 1}])
    assert summarize(study, [result(task, digest, status="failed") for task in study["tasks"]])["useful_primary_tasks"] == 0


def test_critical_defects_and_missing_second_review_do_not_qualify():
    study = manifest(); digest = validate_manifest(study)
    summary = summarize(study, [result(task, digest, score=5, critical=True) for task in study["tasks"]])
    assert summary["useful_primary_tasks"] == 0 and len(summary["missing_human_review_task_ids"]) == 48


def test_tuned_or_duplicate_tasks_are_not_held_out():
    study = manifest(); study["development_task_ids"] = [study["tasks"][0]["task_id"]]
    with pytest.raises(ValueError): validate_manifest(study)
    study = manifest(); study["tasks"][1]["brief"] = study["tasks"][0]["brief"]
    with pytest.raises(ValueError): validate_manifest(study)
