"""Synthetic bookkeeping inputs, never actual visual-study evidence."""
from copy import deepcopy

import pytest

from experiments.evaluation import fingerprint
from experiments.visual_evaluation import ARMS, build_plan, review_packet, summarize, validate_manifest


def manifest():
    return {"schema_version": "1.0", "kind": "matched_raster_improvement", "status": "frozen", "frozen_at": "2026-11-16T00:00:00+00:00", "domain": "synthetic unit-test diagrams", "blinding_seed": "synthetic-test-only", "briefs": [{"brief_id": f"b{index}", "brief": {"title": f"Synthetic test {index}", "purpose": f"Depict exactly {index + 1} labeled nodes"}, "locked_requirements": ["Legible labels"], "human_rubric": "Record defects and actual preference"} for index in range(12)], "repeated_brief_ids": [f"b{index}" for index in range(4)], "double_review_brief_ids": [f"b{index}" for index in range(6)], "configuration_fingerprints": {arm: fingerprint(arm) for arm in ARMS}, "per_trial_limits": {"inference_calls": 24, "image_submissions": 4, "polls": 60, "active_seconds": 1800, "maximum_usd": "2"}, "batch_limits": {"inference_calls": 1440, "image_submissions": 240, "polls": 3600, "maximum_usd": "120"}, "benefit_and_regression_rule": "Synthetic test rule: independent unanimous preference and no hard regression", "acceptable_extra_cost_and_wait": "Synthetic test cap only; no real study conducted"}


def output(trial, *, status="completed", preference="final", regression=False, reviewers=2):
    initial = fingerprint("synthetic pixels " + trial["matched_group_id"])
    final = initial if trial["arm"] == "generation_only" else fingerprint("synthetic final " + trial["trial_id"])
    return {"trial_id": trial["trial_id"], "dataset_fingerprint": trial["dataset_fingerprint"], "configuration_fingerprint": trial["configuration_fingerprint"], "status": status, "initial_candidate_hash": initial, "final_candidate_hash": final, "recorded_at": "2026-11-16T01:00:00+00:00", "usage": {"inference_calls": 2, "image_submissions": 1, "polls": 0, "active_seconds": 30, "reported_usd": ".05", "unknown_exposure_usd": "0"}, "user_selection": "undecided", "original_grades": [{"grade_id": f"grade_{trial['trial_id']}_{index}", "reviewer_id": f"synthetic_reviewer_{index}", "initial_candidate_hash": initial, "final_candidate_hash": final, "pixel_packet_hash": fingerprint(review_packet(manifest(), trial, initial, final)), "timestamp": "2026-11-16T02:00:00+00:00", "hard_regression": regression, "preference": preference, "resolved_defects_and_regressions": ["Synthetic bookkeeping assertion only"]} for index in range(reviewers)]}


def test_twelve_matched_briefs_four_fixed_repeats_make_sixty_trials_without_release():
    data = manifest()
    plan = build_plan(data)
    assert len(plan) == 60
    assert all(sum(trial["arm"] == arm for trial in plan) == 20 for arm in ARMS)
    result = summarize(data, [output(trial) for trial in plan])
    assert result["phase_8_passed"] is False
    assert all(arm["primary_denominator"] == 12 and arm["observed_primary"] == 12 for arm in result["arms"].values())


@pytest.mark.parametrize("change", ["eleven_briefs", "duplicate_brief", "three_repeats", "insufficient_double_review", "higher_call_limit"])
def test_prospective_manifest_rejects_inflated_or_unbounded_study(change):
    data = manifest()
    if change == "eleven_briefs": data["briefs"].pop()
    elif change == "duplicate_brief": data["briefs"][1]["brief"] = data["briefs"][0]["brief"]
    elif change == "three_repeats": data["repeated_brief_ids"].pop()
    elif change == "insufficient_double_review": data["double_review_brief_ids"].pop()
    else: data["per_trial_limits"]["inference_calls"] = 25
    with pytest.raises(ValueError): validate_manifest(data)


def test_matched_arms_cannot_use_favorable_replacement_initial_pixels():
    data = manifest()
    plan = build_plan(data)
    one, two = output(plan[0]), output(plan[1])
    two["initial_candidate_hash"] = fingerprint("favorable replacement")
    with pytest.raises(ValueError, match="identical initial"):
        summarize(data, [one, two])


def test_trial_output_cannot_replace_original_or_change_frozen_identity():
    data = manifest()
    item = output(build_plan(data)[0])
    with pytest.raises(ValueError, match="replaced"):
        summarize(data, [item, deepcopy(item)])
    item["configuration_fingerprint"] = fingerprint("retuned")
    with pytest.raises(ValueError, match="frozen"):
        summarize(data, [item])


def test_failed_and_missing_primaries_unknown_cost_and_adverse_repeats_are_retained():
    data = manifest()
    plan = build_plan(data)
    primary = output(plan[2], status="failed", reviewers=0)
    primary["usage"]["unknown_exposure_usd"] = "1"
    repeat = output(next(trial for trial in plan if trial["arm"] == "multi_critic_revision" and trial["repeat_index"] == 1), regression=True, reviewers=1)
    result = summarize(data, [primary, repeat])
    arm = result["arms"]["multi_critic_revision"]
    assert arm["observed_primary"] == 1 and len(arm["missing_or_unsuccessful_primary"]) == 12
    assert result["usage"]["unknown_exposure_usd"] == "1"
    assert repeat["trial_id"] in result["original_hard_regression_trial_ids"]
    assert repeat["trial_id"] in result["incompletely_reviewed_trial_ids"]
    assert not arm["human_prefers_final_primary"]


def test_human_packet_hashes_and_original_neutral_decline_judgments_remain_separate():
    data = manifest()
    trial = build_plan(data)[2]
    item = output(trial, preference="equal")
    item["user_selection"] = "decline"
    result = summarize(data, [item])["arms"]["multi_critic_revision"]
    assert result["declined_primary"] == ["b0"] and not result["human_prefers_final_primary"]
    item["original_grades"][0]["final_candidate_hash"] = fingerprint("different pixels")
    with pytest.raises(ValueError, match="exact initial/final"):
        summarize(data, [item])


def test_exceeded_limits_stay_visible_instead_of_dropping_unfavorable_output():
    data = manifest()
    item = output(build_plan(data)[0])
    item["usage"].update(inference_calls=25, active_seconds=1801, unknown_exposure_usd="121")
    result = summarize(data, [item])
    assert result["observed_trials"] == 1 and result["per_trial_limit_violations"] == [item["trial_id"]]
    assert result["batch_within_declared_limits"] is False and result["phase_8_passed"] is False


def test_unrelated_well_formed_human_packet_cannot_count_as_the_frozen_review():
    data = manifest()
    item = output(build_plan(data)[2])
    item["original_grades"][0]["pixel_packet_hash"] = fingerprint("unrelated review")
    with pytest.raises(ValueError, match="frozen brief"):
        summarize(data, [item])


def test_per_arm_and_matched_cost_wait_preserve_unknown_and_failed_observations():
    data = manifest()
    plan = build_plan(data)
    baseline, candidate = output(plan[0]), output(plan[2], status="failed", reviewers=0)
    candidate["usage"].update(active_seconds=90, unknown_exposure_usd=".4")
    result = summarize(data, [baseline, candidate])
    measured = result["per_arm_usage"]["multi_critic_revision"]
    assert measured["successful_trials"] == 0 and measured["observed_trials"] == 1
    assert measured["successful_active_seconds_p50"] is None
    assert measured["unknown_exposure_usd"] == "0.4" and measured["active_seconds_observed"] == 90
    pair = result["paired_additional_cost_and_active_time"][0]
    assert pair["candidate_status"] == "failed" and pair["additional_conservative_usd"] == "0.40"
    assert pair["additional_active_seconds"] == 60 and pair["primary"]
