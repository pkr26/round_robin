"""Draft fixed-evidence evaluation tools. These tools cannot invoke a provider.

Fictional fixtures prepare a study; they do not freeze it, fund it, validate
model quality, or satisfy a phase gate. Human grades remain append-only records.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
from typing import Any, Iterable, Mapping


DATASET_PATH = Path(__file__).parent / "fixtures" / "evaluation_tasks.json"
WORKFLOWS = ("single_provider", "independent_review", "bounded_debate")
STRATA = {
    "budget_lifecycle": 4,
    "deadline_feasibility": 3,
    "missing_stale_quote": 3,
    "preference_status_quo": 3,
    "misleading_shared_evidence": 3,
}
_IDENTITIES = re.compile(
    r"\b(?:Google Gemini|Google|OpenAI|Anthropic|Claude|Gemini|ChatGPT|"
    r"single[_ -]provider|independent[_ -]review|bounded[_ -]debate)\b",
    re.IGNORECASE,
)


def fingerprint(value: Any) -> str:
    """Hash the complete canonical input; nonfinite JSON numbers are invalid."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_dataset(path: Path = DATASET_PATH) -> dict[str, Any]:
    dataset = json.loads(path.read_text())
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: Mapping[str, Any]) -> None:
    if dataset.get("status") == "frozen_human_reviewed":
        from experiments.study_runner import FrozenStudyManifest
        FrozenStudyManifest.model_validate(dataset)
        return
    if dataset.get("status") != "draft_fictional_not_frozen":
        raise ValueError("Preparation requires a visibly fictional, unfrozen dataset")
    tasks = dataset["tasks"]
    if len(tasks) != 16 or Counter(t["stratum"] for t in tasks) != Counter(STRATA):
        raise ValueError("The study requires exactly 16 tasks in the declared 4/3/3/3/3 strata")
    ids = [task["task_id"] for task in tasks]
    if any(not isinstance(identity, str) or not identity.strip() for identity in ids) or len(set(ids)) != 16:
        raise ValueError("Task identities must be unique")
    repeats = dataset["repeated_task_ids"]
    dual = dataset["double_review_task_ids"]
    if len(set(repeats)) != 5 or len(repeats) != 5 or not set(repeats) <= set(ids):
        raise ValueError("Exactly five fixed tasks receive three executions")
    if len(set(dual)) < 8 or len(set(dual)) != len(dual) or not set(dual) <= set(ids):
        raise ValueError("At least eight fixed tasks need two independent reviewers")
    for task in tasks:
        if not 2 <= len(task["options"]) <= 4:
            raise ValueError("Each task requires two to four options")
        if not any(option["is_status_quo"] for option in task["options"]):
            raise ValueError("Each fictional task must expose its status quo alternative")
        for key in ("hard_requirements", "acceptable_outcomes", "critical_dependencies", "checked_snapshots"):
            if not task.get(key):
                raise ValueError(f"Task {task['task_id']} lacks {key}")
        source_ids = [s["source_id"] for s in task["checked_snapshots"]]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Snapshot identities must be unique within a task")
        for snapshot in task["checked_snapshots"]:
            if snapshot.get("origin") != "fictional_fixture" or not snapshot.get("passage"):
                raise ValueError("Snapshots must contain an exact fictional passage")
        if task.get("version") != 1 or not task.get("brief"):
            raise ValueError("Task version and brief are required")


@dataclass(frozen=True)
class TrialSpec:
    trial_id: str
    task_id: str
    task_version: int
    workflow: str
    repeat_index: int
    primary: bool
    dataset_fingerprint: str


def build_trial_plan(dataset: Mapping[str, Any]) -> list[TrialSpec]:
    validate_dataset(dataset)
    digest = fingerprint(dataset)
    trials = []
    for workflow in WORKFLOWS:
        for task in dataset["tasks"]:
            repetitions = 3 if task["task_id"] in dataset["repeated_task_ids"] else 1
            for index in range(repetitions):
                trials.append(TrialSpec(
                    trial_id=f"{task['task_id']}.v{task['version']}.{workflow}.r{index}",
                    task_id=task["task_id"], task_version=task["version"],
                    workflow=workflow, repeat_index=index, primary=index == 0,
                    dataset_fingerprint=digest,
                ))
    return trials


@dataclass(frozen=True)
class TrialOutput:
    trial_id: str
    output_id: str
    text: str
    status: str
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in {"completed", "limited", "failed", "cancelled", "missing"}:
            raise ValueError("Unknown trial outcome")
        if not all(isinstance(value, str) and value.strip() for value in (self.trial_id, self.output_id, self.configuration_fingerprint)) or not isinstance(self.text, str):
            raise ValueError("Output and configuration identities are required")


def _validate_outputs(dataset: Mapping[str, Any], outputs: Iterable[TrialOutput]) -> dict[str, TrialOutput]:
    planned = {trial.trial_id for trial in build_trial_plan(dataset)}
    found: dict[str, TrialOutput] = {}
    output_ids: set[str] = set()
    for output in outputs:
        if output.trial_id not in planned:
            raise ValueError("Output does not belong to this versioned trial plan")
        if output.trial_id in found or output.output_id in output_ids:
            raise ValueError("An execution cannot be replaced by a favorable output")
        found[output.trial_id] = output
        output_ids.add(output.output_id)
    return found


def build_blinded_packets(
    dataset: Mapping[str, Any], outputs: Iterable[TrialOutput], *, seed: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Produce public packets and a separate private identity mapping.

    Metadata is allowlisted and common provider/arm names are redacted from
    public text. A human must inspect packets for indirect identity clues before
    a real study. Reusing a seed/input makes packet order and tokens reproducible.
    """
    found = _validate_outputs(dataset, outputs)
    trials = {t.trial_id: t for t in build_trial_plan(dataset)}
    tasks = {t["task_id"]: t for t in dataset["tasks"]}
    packets, identities = [], {}
    for trial_id in sorted(found):
        output, trial = found[trial_id], trials[trial_id]
        public_text = _IDENTITIES.sub("[identity withheld]", output.text)
        token = "packet_" + fingerprint([seed, trial_id, output.output_id])[:24]
        public_hash = fingerprint(public_text)
        task = tasks[trial.task_id]
        typed_brief = task["brief"] if isinstance(task["brief"], dict) else None
        packets.append({
            "packet_token": token,
            "label": "Frozen fixed-evidence study; identities withheld and packet inspection required" if dataset.get("status") == "frozen_human_reviewed" else "Fictional preparation; not live study evidence",
            "task_id": trial.task_id, "task_version": trial.task_version,
            "brief": task["brief"], "options": task.get("options", typed_brief.get("options") if typed_brief else []),
            "hard_requirements": task.get("hard_requirements", typed_brief.get("hard_requirements") if typed_brief else []),
            "acceptable_outcomes": task["acceptable_outcomes"],
            "critical_dependencies": task["critical_dependencies"],
            "checked_snapshots": task["checked_snapshots"],
            "output": {"text": public_text, "hash": public_hash, "status": output.status},
        })
        identities[token] = {
            **asdict(trial), "output_id": output.output_id,
            "original_output_hash": fingerprint(output.text),
            "public_output_hash": public_hash,
            "configuration_fingerprint": output.configuration_fingerprint,
            "original_status": output.status,
        }
    random.Random(seed).shuffle(packets)
    return packets, {
        "visibility": "private_reviewer_identity_mapping",
        "dataset_fingerprint": fingerprint(dataset), "identities": identities,
        "packet_inspection_required": True, "phase_passed": False,
    }


def save_blinded_packets(
    destination: Path, packets: list[dict[str, Any]], private_mapping: Mapping[str, Any],
) -> tuple[Path, Path]:
    """Save immutable, separate reviewer/private files with private permissions.

    Existing artifacts are never replaced. A crash may leave only the private
    map; it never publishes a reviewer packet before its mapping is durable.
    """
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_path = destination / "private_identity_mapping.json"
    public_path = destination / "reviewer_packets.json"
    if private_path.exists() or public_path.exists():
        raise FileExistsError("Reviewer artifacts already exist; retain the original study version")
    for path, value in ((private_path, private_mapping), (public_path, packets)):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # fsync(file) alone does not make its directory entry durable. The
        # identity map must survive a crash before reviewer packets appear.
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return public_path, private_path


@dataclass(frozen=True)
class ManualGrade:
    grade_id: str
    packet_token: str
    task_id: str
    task_version: int
    output_hash: str
    reviewer_id: str
    timestamp: str
    hard_constraint_findings: tuple[str, ...]
    passage_support_findings: tuple[str, ...]
    usefulness: int
    next_action_judgment: str
    hidden_critical_defect: bool = False
    critical_defect_reason: str | None = None
    adjudication_of: tuple[str, ...] = ()
    adjudication_reason: str | None = None

    def __post_init__(self) -> None:
        identities = (self.grade_id, self.packet_token, self.task_id, self.output_hash, self.reviewer_id, self.next_action_judgment)
        if not all(isinstance(value, str) and value.strip() for value in identities) or type(self.task_version) is not int or self.task_version < 1:
            raise ValueError("A grade needs complete typed task/output/reviewer identity and next-action judgment")
        for field in ("hard_constraint_findings", "passage_support_findings", "adjudication_of"):
            values = getattr(self, field)
            if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("Grade findings and adjudication identities must be arrays of nonblank strings")
            object.__setattr__(self, field, tuple(values))
        if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (self.critical_defect_reason, self.adjudication_reason)):
            raise ValueError("Grade reasons must be nonblank text when supplied")
        if type(self.usefulness) is not int or not 1 <= self.usefulness <= 5:
            raise ValueError("Manual usefulness must be an integer from 1 through 5")
        if type(self.hidden_critical_defect) is not bool:
            raise ValueError("Critical defect state must be boolean")
        if self.hidden_critical_defect and not self.critical_defect_reason:
            raise ValueError("Critical defects require an explicit reason")
        if self.hidden_critical_defect and self.usefulness >= 4:
            raise ValueError("A hidden critical defect disqualifies a usefulness score of 4 or 5")
        if not self.hard_constraint_findings or not self.passage_support_findings:
            raise ValueError("Constraint and passage-support findings are required")
        if not isinstance(self.timestamp, str):
            raise ValueError("Grade timestamp must be a timezone-aware ISO string")
        if datetime.fromisoformat(self.timestamp.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Grade timestamp must include its timezone")
        if bool(self.adjudication_of) != bool(self.adjudication_reason):
            raise ValueError("Adjudication preserves original IDs and an explicit reason")
        if self.adjudication_of and (len(self.adjudication_of) < 2 or len(set(self.adjudication_of)) != len(self.adjudication_of) or self.grade_id in self.adjudication_of):
            raise ValueError("Adjudication requires distinct retained original grade identities")


def grade_record_schema() -> dict[str, Any]:
    """Machine-readable shape for original and appended adjudication records."""
    strings = ("grade_id", "packet_token", "task_id", "output_hash", "reviewer_id", "next_action_judgment")
    properties: dict[str, Any] = {key: {"type": "string", "minLength": 1, "pattern": r"\S"} for key in strings}
    properties.update({
        "task_version": {"type": "integer", "minimum": 1},
        "timestamp": {"type": "string", "format": "date-time"},
        "hard_constraint_findings": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": r"\S"}},
        "passage_support_findings": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": r"\S"}},
        "usefulness": {"type": "integer", "minimum": 1, "maximum": 5},
        "hidden_critical_defect": {"type": "boolean"},
        "critical_defect_reason": {"type": ["string", "null"], "pattern": r"\S"},
        "adjudication_of": {"type": "array", "uniqueItems": True, "items": {"type": "string", "pattern": r"\S"}, "anyOf": [{"maxItems": 0}, {"minItems": 2}]},
        "adjudication_reason": {"type": ["string", "null"], "pattern": r"\S"},
    })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Manual evaluation grade",
        "type": "object", "additionalProperties": False, "properties": properties,
        "required": list(strings) + ["task_version", "timestamp", "hard_constraint_findings",
                                     "passage_support_findings", "usefulness"],
        "allOf": [{"if": {"properties": {"hidden_critical_defect": {"const": True}},
                            "required": ["hidden_critical_defect"]},
                   "then": {"required": ["critical_defect_reason"],
                            "properties": {"usefulness": {"maximum": 3},
                                           "critical_defect_reason": {"type": "string", "minLength": 1}}}},
                  {"if": {"properties": {"adjudication_of": {"minItems": 1}}, "required": ["adjudication_of"]},
                   "then": {"required": ["adjudication_reason"], "properties": {"adjudication_reason": {"type": "string", "pattern": r"\S"}}},
                   "else": {"properties": {"adjudication_reason": {"type": "null"}}}}],
    }


def summarize_workflow(
    dataset: Mapping[str, Any], workflow: str, outputs: Iterable[TrialOutput],
    grades: Iterable[ManualGrade], private_mapping: Mapping[str, Any],
) -> dict[str, Any]:
    if workflow not in WORKFLOWS:
        raise ValueError("Unknown workflow")
    found = _validate_outputs(dataset, outputs)
    if private_mapping.get("dataset_fingerprint") != fingerprint(dataset):
        raise ValueError("Reviewer mapping belongs to another dataset version")
    identities = private_mapping["identities"]
    planned = {trial.trial_id: trial for trial in build_trial_plan(dataset)}
    by_trial: dict[str, list[ManualGrade]] = defaultdict(list)
    grade_ids, reviewer_slots = set(), set()
    all_grades = list(grades)
    for grade in all_grades:
        if "approved_reviewer_ids" in private_mapping and grade.reviewer_id not in private_mapping["approved_reviewer_ids"]:
            raise ValueError("Grade reviewer is outside the frozen independent reviewer plan")
        identity = identities.get(grade.packet_token)
        if not identity or identity["trial_id"] not in found:
            raise ValueError("Grade does not resolve to a retained original output")
        output = found[identity["trial_id"]]
        trial = planned[identity["trial_id"]]
        if (any(identity.get(key) != value for key, value in asdict(trial).items())
                or identity["original_output_hash"] != fingerprint(output.text)
                or identity["output_id"] != output.output_id
                or identity["original_status"] != output.status
                or identity["configuration_fingerprint"] != output.configuration_fingerprint
                or identity["public_output_hash"] != fingerprint(_IDENTITIES.sub("[identity withheld]", output.text))
                or identity["public_output_hash"] != grade.output_hash
                or identity["task_id"] != grade.task_id
                or identity["task_version"] != grade.task_version):
            raise ValueError("Grade task/output version mismatch")
        slot = (trial.trial_id, grade.reviewer_id, bool(grade.adjudication_of))
        if grade.grade_id in grade_ids or slot in reviewer_slots:
            raise ValueError("Original grades cannot be overwritten or counted twice")
        grade_ids.add(grade.grade_id)
        reviewer_slots.add(slot)
        by_trial[identity["trial_id"]].append(grade)
    for records in by_trial.values():
        originals = [g for g in records if not g.adjudication_of]
        adjudications = [g for g in records if g.adjudication_of]
        if len(adjudications) > 1:
            raise ValueError("Conflicting adjudications need an explicit versioned study decision")
        if adjudications and (len(originals) < 2 or set(adjudications[0].adjudication_of)
                              != {g.grade_id for g in originals}):
            raise ValueError("Adjudication must retain all independent original grades for the same exact output")
        if adjudications and datetime.fromisoformat(adjudications[0].timestamp.replace("Z", "+00:00")) < max(datetime.fromisoformat(grade.timestamp.replace("Z", "+00:00")) for grade in originals):
            raise ValueError("Adjudication cannot predate an original review")
    useful, reviewed, missing, critical = [], [], [], []
    for trial in build_trial_plan(dataset):
        if trial.workflow != workflow or not trial.primary:
            continue
        output = found.get(trial.trial_id)
        records = by_trial[trial.trial_id]
        originals = [g for g in records if not g.adjudication_of]
        adjudications = [g for g in records if g.adjudication_of]
        required = 2 if trial.task_id in dataset["double_review_task_ids"] else 1
        if any(g.hidden_critical_defect for g in originals):
            required = max(required, 2)
            critical.append(trial.task_id)
        if not output or output.status in {"failed", "cancelled", "missing"} or len(originals) < required:
            missing.append(trial.task_id)
            continue
        selected = originals
        if adjudications:
            adjudication = adjudications[0]
            selected = [adjudication]
        reviewed.append(trial.task_id)
        if all(g.usefulness >= 4 and not g.hidden_critical_defect for g in selected):
            useful.append(trial.task_id)
    repeats = [t for t in build_trial_plan(dataset) if t.workflow == workflow and not t.primary]
    repeat_critical = [t.trial_id for t in repeats
                       if any(g.hidden_critical_defect and not g.adjudication_of for g in by_trial[t.trial_id])]
    repeat_reviewed = []
    for trial in repeats:
        originals = [g for g in by_trial[trial.trial_id] if not g.adjudication_of]
        required = 2 if trial.task_id in dataset["double_review_task_ids"] or trial.trial_id in repeat_critical else 1
        if trial.trial_id in found and found[trial.trial_id].status not in {"failed", "cancelled", "missing"} and len(originals) >= required:
            repeat_reviewed.append(trial.trial_id)
    return {
        "workflow": workflow, "dataset_fingerprint": fingerprint(dataset),
        "mode": "frozen_fixed_evidence_study" if dataset.get("status") == "frozen_human_reviewed" else "fictional_fixed_evidence_preparation", "primary_denominator": 16,
        "useful_primary_count": len(useful), "required_useful_primary": 13,
        "numerical_gate_met": len(useful) >= 13,
        "primary_review_complete": len(reviewed) == 16,
        "repeat_review_complete": len(repeat_reviewed) == len(repeats),
        "review_complete": len(reviewed) == 16 and len(repeat_reviewed) == len(repeats),
        "useful_task_ids": useful, "unreviewed_or_failed_task_ids": missing,
        "original_critical_defect_task_ids": critical,
        "repeat_critical_defect_trial_ids": repeat_critical,
        "repeat_outputs_retained": sum(t.trial_id in found for t in repeats),
        "repeat_denominator_contribution": 0, "phase_passed": False,
        "pending_gates": ["real-user discovery/comprehension", "funded frozen real-provider study",
                          "benefit over strong baseline", "protocol decision", "indirect identity inspection"],
    }


def deadline_attainment(trials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Use every admitted trial as denominator, retaining failed/missing outcomes."""
    rows = list(trials)
    for row in rows:
        if type(row.get("admitted")) is not bool or type(row.get("mandatory_work_complete", False)) is not bool:
            raise ValueError("Admission and mandatory completion observations must be explicit booleans")
    admitted = [row for row in rows if row["admitted"]]
    first, complete_120, complete_300 = 0, 0, 0
    successful_times = []
    for row in admitted:
        for field in ("active_seconds", "first_useful_active_seconds"):
            value = row.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError("Timing observations must be finite, nonnegative seconds")
        if row["status"] != "completed" or not row.get("mandatory_work_complete", False):
            continue
        seconds = row.get("active_seconds")
        if seconds is None:
            continue
        successful_times.append(seconds)
        useful_at = row.get("first_useful_active_seconds")
        if useful_at is not None and useful_at > seconds:
            raise ValueError("First qualifying content cannot follow recorded completion")
        first += useful_at is not None and useful_at <= 60
        complete_120 += seconds <= 120
        complete_300 += seconds <= 300
    n = len(admitted)
    ordered = sorted(successful_times)
    return {
        "admitted_trials": n, "never_admitted_trials": len(rows) - n,
        "first_useful_within_60": {"count": first, "denominator": n, "attainment": first / n if n else None},
        "complete_within_120": {"count": complete_120, "denominator": n, "attainment": complete_120 / n if n else None},
        "complete_within_300": {"count": complete_300, "denominator": n, "attainment": complete_300 / n if n else None},
        "outcomes": dict(Counter(row["status"] for row in admitted)),
        "successful_run_count": len(ordered),
        "successful_p50_active_seconds": ordered[math.ceil(len(ordered) * .5) - 1] if ordered else None,
        "successful_p90_active_seconds": ordered[math.ceil(len(ordered) * .9) - 1] if ordered else None,
        "phase_passed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "plan", "grade-schema"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset = load_dataset()
    plan = build_trial_plan(dataset)
    if args.command == "grade-schema":
        result: Any = grade_record_schema()
    elif args.command == "plan":
        result = {"status": "draft_fictional_not_frozen", "paid_access_enabled": False,
                  "dataset_fingerprint": fingerprint(dataset), "trials": [asdict(t) for t in plan]}
    else:
        result = {"status": dataset["status"], "task_count": 16, "trials_per_workflow": 26,
                  "total_trials": len(plan), "dataset_fingerprint": fingerprint(dataset),
                  "paid_access_enabled": False, "phase_passed": False}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
