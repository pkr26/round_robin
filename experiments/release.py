"""Prospective 48-task end-to-end release evaluation; no provider execution.

Real held-out tasks and independent human grades are supplied as immutable files.
This module validates them and counts missing/failed trials conservatively. It
never fabricates tasks, observations, grades or a release decision.
"""

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

from .evaluation import deadline_attainment, fingerprint
from .cases import substantive_fingerprint, reject_development_overlap

STRATA = ("budget_lifecycle", "deadline_hard_constraints", "missing_stale_inputs", "preferences_status_quo", "misleading_shared_evidence", "clarification_tool_provider_limits")


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Evaluation timestamps must be timezone-aware ISO strings")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Evaluation timestamps must include an explicit timezone")
    return result


def validate_manifest(manifest: dict) -> str:
    if manifest.get("mode") != "held_out_end_to_end" or manifest.get("schema_version") != "1.0":
        raise ValueError("Release evaluation requires an end-to-end held-out manifest")
    tasks = manifest.get("tasks", [])
    if len(tasks) != 48 or Counter(task.get("stratum") for task in tasks) != Counter({key: 8 for key in STRATA}):
        raise ValueError("Declare exactly 48 independent tasks, eight in each release stratum")
    substantive = [substantive_fingerprint(task.get("brief"), {key: task.get(key) for key in ("hard_requirements", "critical_dependencies", "acceptable_outcomes", "evidence_manifest")}) for task in tasks]
    if any(not isinstance(task.get("task_id"), str) or not task["task_id"].strip() for task in tasks) or len({task.get("task_id") for task in tasks}) != 48 or len(set(substantive)) != 48:
        raise ValueError("Independent task identities and briefs cannot be duplicated to inflate the denominator")
    reject_development_overlap(substantive, manifest.get("development_case_fingerprints", []))
    development = set(manifest.get("development_task_ids", []))
    if development & {task["task_id"] for task in tasks}:
        raise ValueError("A tuned or retired failure requires fresh held-out coverage")
    for task in tasks:
        for field in ("brief", "hard_requirements", "critical_dependencies", "acceptable_outcomes", "evidence_manifest"):
            if not task.get(field):
                raise ValueError(f"Every task must preregister its {field}")
    if not manifest.get("configuration_fingerprint") or not manifest.get("protocol_decision_id"):
        raise ValueError("Bind the selected protocol and immutable execution configuration before evaluation")
    _timestamp(manifest.get("frozen_at"))
    return fingerprint(manifest)


def summarize(manifest: dict, records: list[dict]) -> dict:
    dataset_hash = validate_manifest(manifest)
    planned = {task["task_id"] for task in manifest["tasks"]}
    found, useful, critical, grades_missing = {}, [], [], []
    grade_ids = set()
    frozen_at = _timestamp(manifest["frozen_at"])
    for record in records:
        identity = record.get("task_id")
        if identity not in planned or identity in found or type(record.get("repeat_index", 0)) is not int or record.get("repeat_index", 0) != 0:
            raise ValueError("A primary task cannot be replaced by a repeated or unrelated output")
        if record.get("dataset_fingerprint") != dataset_hash or record.get("configuration_fingerprint") != manifest["configuration_fingerprint"]:
            raise ValueError("Result identity differs from the frozen study")
        if record.get("status") not in {"completed", "limited", "failed", "cancelled", "missing"} or not record.get("output_hash"):
            raise ValueError("Retain explicit outcome and output identity for every observed trial")
        found[identity] = record
        recorded_at = _timestamp(record.get("recorded_at"))
        if recorded_at < frozen_at:
            raise ValueError("A prospective freeze cannot follow an observed output")
        grades = record.get("original_grades", [])
        if len({grade.get("reviewer_id") for grade in grades}) != len(grades) or any(not grade.get("reviewer_id") for grade in grades):
            raise ValueError("Independent original grades require distinct reviewer identities")
        for grade in grades:
            if type(grade.get("usefulness")) is not int or not 1 <= grade["usefulness"] <= 5 or not grade.get("grade_id") or not grade.get("timestamp"):
                raise ValueError("Human grades need original identity, timestamp and a usefulness score from one to five")
            if grade["grade_id"] in grade_ids or not isinstance(grade["grade_id"], str) or type(grade.get("critical_defect", False)) is not bool:
                raise ValueError("Original grade identities must be unique and critical-defect judgments must be boolean")
            grade_ids.add(grade["grade_id"])
            if _timestamp(grade["timestamp"]) < recorded_at or grade.get("output_hash") != record["output_hash"]:
                raise ValueError("A human grade must follow and identify the retained frozen output")
            if grade.get("critical_defect") and not grade.get("critical_defect_reason"):
                raise ValueError("Critical defects require an attributable reason")
        has_critical = any(grade.get("critical_defect") for grade in grades)
        if has_critical:
            critical.append(identity)
        adjudication = record.get("adjudication")
        if adjudication:
            if len(grades) < 2 or not adjudication.get("reason") or set(adjudication.get("original_grade_ids", [])) != {grade["grade_id"] for grade in grades} or type(adjudication.get("usefulness")) is not int or not 1 <= adjudication["usefulness"] <= 5:
                raise ValueError("Adjudication must preserve and reference every original grade")
            if not isinstance(adjudication.get("grade_id"), str) or not adjudication["grade_id"] or adjudication["grade_id"] in grade_ids or not adjudication.get("reviewer_id") or adjudication.get("output_hash") != record["output_hash"] or type(adjudication.get("critical_defect", has_critical)) is not bool:
                raise ValueError("Adjudication needs its own reviewer, grade and exact output identities")
            if _timestamp(adjudication.get("timestamp")) < max(_timestamp(grade["timestamp"]) for grade in grades):
                raise ValueError("Adjudication cannot predate any original grade")
            grade_ids.add(adjudication["grade_id"])
        # Suspected critical defects require two originals before adjudication;
        # disagreement remains conservative until a separate reasoned record.
        if not grades or has_critical and len(grades) < 2:
            grades_missing.append(identity)
            continue
        score = min(grade["usefulness"] for grade in grades)
        if adjudication:
            score = adjudication["usefulness"]
            has_critical = adjudication.get("critical_defect", has_critical)
        if record["status"] in {"completed", "limited"} and score >= 4 and not has_critical:
            useful.append(identity)
    return {"mode": "held_out_end_to_end", "dataset_fingerprint": dataset_hash, "independent_primary_tasks": 48, "observed_primary_tasks": len(found), "missing_primary_task_ids": sorted(planned - found.keys()), "missing_human_review_task_ids": sorted(grades_missing), "useful_primary_tasks": len(useful), "numerical_usefulness_target": len(useful) >= 39, "original_critical_defect_task_ids": sorted(critical), "phase_5_passed": False, "release_decision_required": "Correctness, recovery, privacy, usability, cost and admitted-trial performance evidence must be reviewed separately; a numerical usefulness target alone never publishes a release."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.results:
        print(json.dumps(summarize(manifest, json.loads(args.results.read_text())), indent=2))
    else:
        print(json.dumps({"dataset_fingerprint": validate_manifest(manifest), "status": "manifest_valid", "phase_5_passed": False}, indent=2))


if __name__ == "__main__":
    main()
