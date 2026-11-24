"""Evaluation preparation checks; no live quality or participant evidence."""

from collections import Counter
from dataclasses import replace
from decimal import Decimal
import json
import stat

import pytest

from experiments.evaluation import (
    ManualGrade, TrialOutput, WORKFLOWS, build_blinded_packets, build_trial_plan,
    deadline_attainment, fingerprint, grade_record_schema, load_dataset,
    save_blinded_packets, summarize_workflow, validate_dataset,
)
from scripts.scenario_summary import check_manifest, render_summary


def study(workflow="independent_review", scores=None, failed=()):
    dataset = load_dataset()
    trials = [t for t in build_trial_plan(dataset) if t.workflow == workflow]
    outputs = [TrialOutput(t.trial_id, f"output-{index}", f"Candidate result {index}",
                           "failed" if t.task_id in failed and t.primary else "completed",
                           fingerprint({"configuration": 1})) for index, t in enumerate(trials)]
    packets, mapping = build_blinded_packets(dataset, outputs, seed="review-plan-v1")
    grades = []
    for packet in packets:
        identity = mapping["identities"][packet["packet_token"]]
        count = 2 if packet["task_id"] in dataset["double_review_task_ids"] else 1
        score = scores.get(packet["task_id"], 4) if scores and identity["primary"] else 4
        for reviewer in range(count):
            grades.append(ManualGrade(
                grade_id=f"{packet['packet_token']}-reviewer-{reviewer}",
                packet_token=packet["packet_token"], task_id=packet["task_id"],
                task_version=packet["task_version"], output_hash=packet["output"]["hash"],
                reviewer_id=f"reviewer-{reviewer}", timestamp="2026-11-15T10:00:00-07:00",
                hard_constraint_findings=("Fixture requirement inspected",),
                passage_support_findings=("Fictional passage inspected",),
                usefulness=score, next_action_judgment="Fixture next action is explicit",
            ))
    return dataset, outputs, grades, mapping


def test_exact_task_strata_primary_repeat_allocation_and_draft_fingerprints():
    dataset = load_dataset()
    plan = build_trial_plan(dataset)
    assert len(plan) == len({t.trial_id for t in plan}) == 78
    assert Counter(t.workflow for t in plan) == dict.fromkeys(WORKFLOWS, 26)
    assert Counter(t.workflow for t in plan if t.primary) == dict.fromkeys(WORKFLOWS, 16)
    assert Counter(t.workflow for t in plan if not t.primary) == dict.fromkeys(WORKFLOWS, 10)
    assert all(t.dataset_fingerprint == fingerprint(dataset) for t in plan)
    assert dataset["funding"]["paid_access_enabled"] is False
    changed = json.loads(json.dumps(dataset))
    changed["tasks"][0]["hard_requirements"].append("A changed hard requirement")
    assert fingerprint(changed) != fingerprint(dataset)


def test_fictional_cost_snapshots_use_common_horizon_and_unknown_is_not_zero():
    dataset = load_dataset()
    unknown = 0
    for task in dataset["tasks"]:
        costs = task["expected_costs"]
        assert {c["option_id"] for c in costs} == {o["option_id"] for o in task["options"]}
        assert {c["horizon_months"] for c in costs} == {24}
        assert {c["currency"] for c in costs} == {"USD"}
        for cost in costs:
            if cost["upfront"] is None or cost["monthly"] is None:
                assert cost["total"] is None
                unknown += 1
            else:
                assert Decimal(cost["upfront"]) + Decimal(cost["monthly"]) * 24 == Decimal(cost["total"])
    assert unknown == 5


@pytest.mark.parametrize("mutation", [
    lambda d: d["tasks"].pop(),
    lambda d: d["repeated_task_ids"].append("BUD-02"),
    lambda d: d["tasks"][0].update(checked_snapshots=[]),
    lambda d: d.update(status="frozen"),
    lambda d: d["tasks"][0]["options"][2].update(is_status_quo=False),
])
def test_incomplete_or_claimed_frozen_preparation_is_rejected(mutation):
    dataset = load_dataset()
    mutation(dataset)
    with pytest.raises(ValueError):
        validate_dataset(dataset)


def test_packet_generation_is_reproducible_blinded_and_mapping_is_separate():
    dataset = load_dataset()
    trial = build_trial_plan(dataset)[0]
    output = TrialOutput(trial.trial_id, "raw-openai-baseline-id", "OpenAI and Claude with bounded_debate.",
                         "completed", "config-v1")
    packets, private = build_blinded_packets(dataset, [output], seed="seed-v1")
    assert (packets, private) == build_blinded_packets(dataset, [output], seed="seed-v1")
    public = json.dumps(packets)
    assert not any(token in public for token in ("OpenAI", "Claude", "bounded_debate", output.output_id, trial.trial_id))
    assert private["identities"][packets[0]["packet_token"]]["output_id"] == output.output_id
    assert private["packet_inspection_required"] is True
    changed, _ = build_blinded_packets(dataset, [output], seed="seed-v2")
    assert changed[0]["packet_token"] != packets[0]["packet_token"]


def test_original_output_cannot_be_replaced_by_favorable_rerun():
    dataset = load_dataset()
    trial = build_trial_plan(dataset)[0]
    output = TrialOutput(trial.trial_id, "original", "original result", "completed", "config")
    with pytest.raises(ValueError, match="replaced"):
        build_blinded_packets(dataset, [output, replace(output, output_id="new", text="favorable")], seed="seed")


def test_private_mapping_is_saved_separately_with_restricted_permissions_and_no_overwrite(tmp_path):
    dataset, outputs, _, _ = study()
    packets, private = build_blinded_packets(dataset, outputs, seed="stored-packets")
    public_path, private_path = save_blinded_packets(tmp_path / "packets", packets, private)
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert "identities" not in json.loads(public_path.read_text())[0]
    assert json.loads(private_path.read_text())["identities"]
    with pytest.raises(FileExistsError):
        save_blinded_packets(tmp_path / "packets", packets, private)


def test_primary_gate_counts_13_of_16_and_never_declares_a_phase_passed():
    dataset, outputs, grades, mapping = study(scores={"BUD-01": 3, "BUD-02": 3, "BUD-03": 3})
    summary = summarize_workflow(dataset, "independent_review", outputs, grades, mapping)
    assert summary["useful_primary_count"] == 13
    assert summary["primary_denominator"] == 16
    assert summary["numerical_gate_met"] is True
    assert summary["repeat_outputs_retained"] == 10
    assert summary["repeat_denominator_contribution"] == 0
    assert summary["phase_passed"] is False
    assert summary["pending_gates"]


def test_failures_and_favorable_repeats_cannot_inflate_primary_gate():
    dataset, outputs, grades, mapping = study(failed=("BUD-01", "DDL-01", "GAP-01", "PREF-01"))
    summary = summarize_workflow(dataset, "independent_review", outputs, grades, mapping)
    assert summary["useful_primary_count"] == 12
    assert summary["numerical_gate_met"] is False
    assert summary["repeat_outputs_retained"] == 10


def test_missing_second_reviewer_is_pending_and_duplicate_reviewer_cannot_count_twice():
    dataset, outputs, grades, mapping = study()
    missing = [g for g in grades if not (g.task_id == "BUD-01" and g.reviewer_id == "reviewer-1")]
    summary = summarize_workflow(dataset, "independent_review", outputs, missing, mapping)
    assert summary["review_complete"] is False
    assert "BUD-01" in summary["unreviewed_or_failed_task_ids"]
    with pytest.raises(ValueError, match="counted twice"):
        summarize_workflow(dataset, "independent_review", outputs, grades + [replace(grades[0], grade_id="duplicate")], mapping)


def test_hidden_critical_defect_cannot_score_four_or_five_and_original_grade_is_retained():
    dataset, outputs, grades, mapping = study()
    grade = next(g for g in grades if mapping["identities"][g.packet_token]["primary"])
    with pytest.raises(ValueError, match="disqualifies"):
        replace(grade, hidden_critical_defect=True, critical_defect_reason="Hard budget omitted")
    defect = replace(grade, usefulness=2, hidden_critical_defect=True, critical_defect_reason="Hard budget omitted")
    revised = [defect if g.grade_id == defect.grade_id else g for g in grades]
    summary = summarize_workflow(dataset, "independent_review", outputs, revised, mapping)
    assert defect.task_id in summary["original_critical_defect_task_ids"]


def test_repeated_critical_defects_are_retained_without_improving_primary_count():
    dataset, outputs, grades, mapping = study()
    repeated = next(g for g in grades if not mapping["identities"][g.packet_token]["primary"])
    defect = replace(repeated, usefulness=2, hidden_critical_defect=True, critical_defect_reason="Repeated output violates budget")
    records = [defect if g.grade_id == defect.grade_id else g for g in grades]
    summary = summarize_workflow(dataset, "independent_review", outputs, records, mapping)
    assert summary["useful_primary_count"] == 16
    assert mapping["identities"][defect.packet_token]["trial_id"] in summary["repeat_critical_defect_trial_ids"]
    assert summary["phase_passed"] is False


def test_grade_output_config_and_mapping_versions_must_match_original():
    dataset, outputs, grades, mapping = study()
    with pytest.raises(ValueError, match="version mismatch"):
        summarize_workflow(dataset, "independent_review", outputs, [replace(grades[0], output_hash="forged")], mapping)
    changed = [replace(outputs[0], configuration_fingerprint="different")] + outputs[1:]
    with pytest.raises(ValueError, match="version mismatch"):
        summarize_workflow(dataset, "independent_review", changed, grades, mapping)


def test_adjudication_appends_reason_and_original_ids_instead_of_replacing_scores():
    dataset, outputs, grades, mapping = study()
    primary = next(g for g in grades if g.task_id == "BUD-01" and mapping["identities"][g.packet_token]["primary"])
    originals = [g for g in grades if g.packet_token == primary.packet_token]
    low = replace(originals[0], usefulness=3)
    independent = [low if g.grade_id == low.grade_id else g for g in grades]
    before = summarize_workflow(dataset, "independent_review", outputs, independent, mapping)
    assert before["useful_primary_count"] == 15
    adjudicated = replace(primary, grade_id="adjudication-1", reviewer_id="adjudicator", usefulness=4,
                         adjudication_of=tuple(g.grade_id for g in originals),
                         adjudication_reason="Reviewers reconciled the exact quoted horizon")
    after = summarize_workflow(dataset, "independent_review", outputs, independent + [adjudicated], mapping)
    assert after["useful_primary_count"] == 16
    assert independent[grades.index(originals[0])].usefulness == 3


def test_grade_schema_records_findings_timestamp_and_original_adjudication_links():
    schema = grade_record_schema()
    assert schema["additionalProperties"] is False
    assert {"output_hash", "reviewer_id", "timestamp", "hard_constraint_findings", "passage_support_findings"} <= set(schema["required"])
    assert "adjudication_of" in schema["properties"]


def test_deadline_attainment_keeps_failures_cancelled_and_absent_in_denominator():
    rows = [
        {"admitted": True, "status": "completed", "mandatory_work_complete": True, "active_seconds": 110, "first_useful_active_seconds": 35},
        {"admitted": True, "status": "completed", "mandatory_work_complete": True, "active_seconds": 250, "first_useful_active_seconds": 70},
        {"admitted": True, "status": "failed", "active_seconds": 40, "first_useful_active_seconds": 20},
        {"admitted": True, "status": "cancelled", "active_seconds": 10},
        {"admitted": True, "status": "missing"},
        {"admitted": False, "status": "queued"},
        {"admitted": True, "status": "completed", "mandatory_work_complete": False, "active_seconds": 30},
    ]
    result = deadline_attainment(rows)
    assert result["admitted_trials"] == 6 and result["never_admitted_trials"] == 1
    assert result["first_useful_within_60"] == {"count": 1, "denominator": 6, "attainment": 1 / 6}
    assert result["complete_within_120"]["count"] == 1
    assert result["complete_within_300"]["count"] == 2
    assert result["successful_run_count"] == 2
    assert result["successful_p50_active_seconds"] == 110
    assert result["successful_p90_active_seconds"] == 250
    assert deadline_attainment([])["complete_within_120"]["attainment"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_timing_observations_cannot_establish_a_deadline_gate(value):
    with pytest.raises(ValueError):
        deadline_attainment([{"admitted": True, "status": "completed", "mandatory_work_complete": True, "active_seconds": value}])


def test_inventory_preserves_exact_88_core_12_visual_requirements_without_pass_claims():
    manifest = check_manifest()
    assert len(manifest["scenarios"]) == 100
    summary = render_summary(manifest)
    assert "No scenario or phase is declared passed" in summary
    assert all(s["status"] in {"pending", "partial_offline_assertions"} for s in manifest["scenarios"])
    browser = [s for s in manifest["scenarios"] if s.get("preparation_test_paths")]
    assert {"CMD-07", "CMD-13", "RPT-07", "RPT-09"} <= {s["scenario_id"] for s in browser}
    assert all(s["status"] in {"pending", "partial_offline_assertions"} and "durable/API/later obligations remain pending" in s["preparation_scope_note"] for s in browser)
    assert all(s.get("browser_test_paths") == ["tests/e2e/live.spec.ts"] for s in manifest["scenarios"] if s["scenario_id"] in {"CMD-07", "CMD-13", "RPT-07", "RPT-09"})
