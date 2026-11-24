"""Prospective matched raster study bookkeeping; never invokes providers.

Real briefs, immutable image hashes and independent human judgments must be
supplied by the study operator. Missing trials remain in the denominator.
"""

import argparse
from datetime import datetime
from decimal import Context, Decimal, Inexact, InvalidOperation, localcontext
import json
import math
from pathlib import Path
import re

from .evaluation import fingerprint
from .cases import substantive_fingerprint, reject_development_overlap

ARMS = ("generation_only", "one_critic_revision", "multi_critic_revision")
HASH = re.compile(r"[0-9a-f]{64}\Z")
OUTCOMES = {"completed", "limited", "failed", "cancelled", "missing"}


def _is_hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _money(value):
    if isinstance(value, bool) or len(str(value)) > 128:
        raise ValueError("Study money must be bounded numeric input")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Study money must be finite bounded numeric input") from None
    if not result.is_finite() or result < 0 or result > Decimal("1e30") or result.as_tuple().exponent < -64 or len(result.as_tuple().digits) > 64:
        raise ValueError("Study money must be finite, nonnegative and within exact arithmetic bounds")
    return result


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Study timestamps must be timezone-aware ISO strings")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Study timestamps need an explicit timezone")
    return result


def validate_manifest(manifest: dict) -> str:
    if manifest.get("schema_version") != "1.0" or manifest.get("kind") != "matched_raster_improvement" or manifest.get("status") != "frozen":
        raise ValueError("A frozen matched raster study manifest is required")
    _timestamp(manifest["frozen_at"])
    briefs = manifest.get("briefs", [])
    ids = [brief.get("brief_id") for brief in briefs]
    if len(briefs) < 12 or any(not isinstance(identity, str) or not identity.strip() for identity in ids) or len(set(ids)) != len(ids):
        raise ValueError("Freeze at least twelve independent brief identities before spending")
    substantive = [substantive_fingerprint(brief.get("brief"), {key: brief.get(key) for key in ("locked_requirements", "human_rubric")}) for brief in briefs]
    if len(set(substantive)) != len(briefs):
        raise ValueError("Duplicated briefs cannot inflate independent primary coverage")
    reject_development_overlap(substantive, manifest.get("development_case_fingerprints", []))
    if not manifest.get("domain") or any(not brief.get("brief") or not brief.get("locked_requirements") or not brief.get("human_rubric") for brief in briefs):
        raise ValueError("Every brief requires locked requirements and a domain-competent human rubric")
    if not isinstance(manifest.get("blinding_seed"), str) or len(manifest["blinding_seed"]) < 8:
        raise ValueError("Freeze a recorded private blinding seed before allocating packets")
    repeats, dual = manifest.get("repeated_brief_ids", []), manifest.get("double_review_brief_ids", [])
    if len(repeats) != 4 or len(set(repeats)) != 4 or not set(repeats) <= set(ids):
        raise ValueError("Four fixed briefs receive three executions each")
    if len(dual) != len(set(dual)) or len(dual) < math.ceil(len(briefs) / 2) or not set(dual) <= set(ids):
        raise ValueError("Predeclare two independent reviewers for at least half of the briefs")
    configurations = manifest.get("configuration_fingerprints", {})
    if set(configurations) != set(ARMS) or any(not _is_hash(value) for value in configurations.values()):
        raise ValueError("Freeze an immutable configuration for each of the three arms")
    trial = manifest.get("per_trial_limits", {})
    for key, maximum in (("inference_calls", 24), ("image_submissions", 4), ("polls", 60), ("active_seconds", 1800)):
        if type(trial.get(key)) != int or not 0 < trial[key] <= maximum:
            raise ValueError("Each visual trial must retain the product's bounded allowances")
    if _money(trial.get("maximum_usd", "0")) <= 0:
        raise ValueError("Predeclare a positive per-trial currency ceiling")
    batch = manifest.get("batch_limits", {})
    for key in ("inference_calls", "image_submissions", "polls"):
        if type(batch.get(key)) != int or batch[key] <= 0:
            raise ValueError("Declare separate whole-batch inference, submission and polling ceilings")
    if _money(batch.get("maximum_usd", "0")) <= 0 or not manifest.get("benefit_and_regression_rule") or not manifest.get("acceptable_extra_cost_and_wait"):
        raise ValueError("Predeclare batch funding, useful benefit, regressions and acceptable extra cost/wait")
    return fingerprint(manifest)


def build_plan(manifest: dict) -> list[dict]:
    digest = validate_manifest(manifest)
    plan = []
    for brief in manifest["briefs"]:
        for index in range(3 if brief["brief_id"] in manifest["repeated_brief_ids"] else 1):
            # All arms in a matched group must start from the same actual image.
            group = f"{brief['brief_id']}.r{index}"
            for arm in ARMS:
                plan.append({"trial_id": f"{group}.{arm}", "matched_group_id": group, "brief_id": brief["brief_id"], "repeat_index": index, "primary": index == 0, "arm": arm, "dataset_fingerprint": digest, "configuration_fingerprint": manifest["configuration_fingerprints"][arm]})
    return plan


def review_packet(manifest: dict, trial: dict, initial_hash: str, final_hash: str) -> dict:
    """Canonical public packet metadata; the exact two image files accompany it.

    Keep the study manifest/seed and trial-token mapping private. The reviewer
    sees no provider, arm, configuration, trial identity or other critic's views.
    Hashes identify supplied files, not evidence that a human looked at them.
    """
    briefs = {brief["brief_id"]: brief for brief in manifest["briefs"]}
    brief = briefs[trial["brief_id"]]
    if not _is_hash(initial_hash) or not _is_hash(final_hash):
        raise ValueError("A review packet requires exact initial/final pixel hashes")
    return {"schema_version": "visual-human-packet-1", "packet_token": "visual_packet_" + fingerprint([manifest["blinding_seed"], trial["trial_id"], initial_hash, final_hash])[:24], "brief": brief["brief"], "locked_requirements": brief["locked_requirements"], "human_rubric": brief["human_rubric"], "initial_candidate_hash": initial_hash, "final_candidate_hash": final_hash}


def summarize(manifest: dict, records: list[dict]) -> dict:
    # Keep reported and unresolved charges exact even at a declared ceiling.
    # Each monetary operand is bounded by _money; 128 digits preserves their
    # sum and difference without silently releasing a fractional exposure.
    with localcontext(Context(prec=128)) as context:
        context.traps[Inexact] = True
        return _summarize(manifest, records)


def _summarize(manifest: dict, records: list[dict]) -> dict:
    plan = {trial["trial_id"]: trial for trial in build_plan(manifest)}
    found, initial_hashes, grade_ids = {}, {}, set()
    results = {arm: {"primary_denominator": len(manifest["briefs"]), "observed_primary": 0, "missing_or_unsuccessful_primary": [], "incompletely_reviewed_primary": [], "original_hard_regression_primary": [], "human_prefers_final_primary": [], "declined_primary": []} for arm in ARMS}
    totals = {"inference_calls": 0, "image_submissions": 0, "polls": 0, "reported_usd": Decimal(0), "unknown_exposure_usd": Decimal(0)}
    arm_usage = {arm: {"observed_trials": 0, "successful_trials": 0, "active_seconds_observed": 0.0, "successful_active_seconds": [], "reported_usd": Decimal(0), "unknown_exposure_usd": Decimal(0)} for arm in ARMS}
    over_limit, incomplete_reviews, regressions, unknown_trials = [], [], [], []
    for record in records:
        identity = record.get("trial_id")
        if identity not in plan or identity in found:
            raise ValueError("A trial cannot be replaced, duplicated or introduced after freeze")
        trial = plan[identity]
        if any(record.get(key) != trial[key] for key in ("dataset_fingerprint", "configuration_fingerprint")):
            raise ValueError("The output differs from its frozen dataset/configuration")
        status = record.get("status")
        initial, final = record.get("initial_candidate_hash"), record.get("final_candidate_hash")
        if status not in OUTCOMES or any(value is not None and not _is_hash(value) for value in (initial, final)):
            raise ValueError("Preserve an explicit outcome and valid immutable image hashes")
        if status in {"completed", "limited"} and (not initial or not final):
            raise ValueError("A successful image trial requires actual initial and final pixel identities")
        group = trial["matched_group_id"]
        if group in initial_hashes and initial_hashes[group] != initial:
            raise ValueError("Matched arms must use the identical initial candidate, including generation failure")
        initial_hashes[group] = initial
        if trial["arm"] == "generation_only" and final is not None and initial != final:
            raise ValueError("Generation-only cannot silently substitute an edited candidate")
        if _timestamp(record["recorded_at"]) < _timestamp(manifest["frozen_at"]):
            raise ValueError("A prospective freeze cannot follow its observed results")
        usage = record.get("usage", {})
        for key in ("inference_calls", "image_submissions", "polls"):
            if type(usage.get(key)) != int or usage[key] < 0:
                raise ValueError("Preserve actual counted attempts, including failed or uncharged calls")
            totals[key] += usage[key]
            if usage[key] > manifest["per_trial_limits"][key]:
                over_limit.append(identity)
        cost, unknown = _money(usage["reported_usd"]), _money(usage["unknown_exposure_usd"])
        totals["reported_usd"] += cost
        totals["unknown_exposure_usd"] += unknown
        if unknown:
            unknown_trials.append(identity)
        if cost + unknown > _money(manifest["per_trial_limits"]["maximum_usd"]):
            over_limit.append(identity)
        active = usage.get("active_seconds")
        if type(active) not in {int, float} or not math.isfinite(active) or active < 0:
            raise ValueError("Retain finite nonnegative active time for every observed result")
        if active > manifest["per_trial_limits"]["active_seconds"]:
            over_limit.append(identity)
        measured = arm_usage[trial["arm"]]
        measured["observed_trials"] += 1
        measured["active_seconds_observed"] += active
        if not math.isfinite(measured["active_seconds_observed"]):
            raise ValueError("Accumulated active time must remain finite")
        measured["reported_usd"] += cost
        measured["unknown_exposure_usd"] += unknown
        if status in {"completed", "limited"}:
            measured["successful_trials"] += 1
            measured["successful_active_seconds"].append(active)
        grades, reviewers = record.get("original_grades", []), set()
        for grade in grades:
            if not grade.get("grade_id") or grade["grade_id"] in grade_ids or not grade.get("reviewer_id") or grade["reviewer_id"] in reviewers:
                raise ValueError("Independent original grades require unique grade and reviewer identities")
            grade_ids.add(grade["grade_id"])
            reviewers.add(grade["reviewer_id"])
            if not initial or not final or grade.get("initial_candidate_hash") != initial or grade.get("final_candidate_hash") != final or not _is_hash(grade.get("pixel_packet_hash")):
                raise ValueError("Human findings must bind the exact initial/final image packet")
            if grade["pixel_packet_hash"] != fingerprint(review_packet(manifest, trial, initial, final)):
                raise ValueError("Human packet hash differs from the frozen brief, rubric, allocation or pixels")
            if _timestamp(grade["timestamp"]) < _timestamp(record["recorded_at"]):
                raise ValueError("An original human review cannot predate the output")
            if type(grade.get("hard_regression")) is not bool or grade.get("preference") not in {"initial", "final", "equal", "decline"} or not grade.get("resolved_defects_and_regressions"):
                raise ValueError("Record actual defect/regression findings and preference, including neutral or decline")
        if record.get("user_selection", "undecided") not in {"initial", "final", "decline", "undecided"}:
            raise ValueError("User selection is an explicit separate decision")
        regression = any(grade["hard_regression"] for grade in grades)
        required = 2 if trial["brief_id"] in manifest["double_review_brief_ids"] or regression else 1
        complete = len(grades) >= required
        if not complete:
            incomplete_reviews.append(identity)
        if regression:
            regressions.append(identity)
        found[identity] = record
        if not trial["primary"]:
            continue
        result = results[trial["arm"]]
        result["observed_primary"] += 1
        if status not in {"completed", "limited"}:
            result["missing_or_unsuccessful_primary"].append(trial["brief_id"])
        if regression:
            result["original_hard_regression_primary"].append(trial["brief_id"])
        if not complete:
            result["incompletely_reviewed_primary"].append(trial["brief_id"])
        if record.get("user_selection") == "decline" or any(grade["preference"] == "decline" for grade in grades):
            result["declined_primary"].append(trial["brief_id"])
        if complete and not regression and initial != final and status in {"completed", "limited"} and all(grade["preference"] == "final" for grade in grades):
            result["human_prefers_final_primary"].append(trial["brief_id"])
    for trial_id, trial in plan.items():
        if trial["primary"] and trial_id not in found:
            results[trial["arm"]]["missing_or_unsuccessful_primary"].append(trial["brief_id"])
    batch = manifest["batch_limits"]
    batch_within = all(totals[key] <= batch[key] for key in ("inference_calls", "image_submissions", "polls")) and totals["reported_usd"] + totals["unknown_exposure_usd"] <= _money(batch["maximum_usd"])
    by_group = {(plan[key]["matched_group_id"], plan[key]["arm"]): value for key, value in found.items()}
    paired = []
    for trial in plan.values():
        if trial["arm"] == "generation_only":
            continue
        baseline = by_group.get((trial["matched_group_id"], "generation_only"))
        candidate = found.get(trial["trial_id"])
        if baseline is None or candidate is None:
            continue
        def exposure(row):
            return _money(row["usage"]["reported_usd"]) + _money(row["usage"]["unknown_exposure_usd"])
        paired.append({"matched_group_id": trial["matched_group_id"], "arm": trial["arm"], "primary": trial["primary"], "baseline_status": baseline["status"], "candidate_status": candidate["status"], "additional_conservative_usd": str(exposure(candidate) - exposure(baseline)), "additional_active_seconds": candidate["usage"]["active_seconds"] - baseline["usage"]["active_seconds"]})
    for measured in arm_usage.values():
        samples = sorted(measured.pop("successful_active_seconds"))
        measured["successful_active_seconds_p50"] = samples[math.ceil(len(samples) * .5) - 1] if samples else None
        measured["successful_active_seconds_p90"] = samples[math.ceil(len(samples) * .9) - 1] if samples else None
        measured["reported_usd"] = str(measured["reported_usd"])
        measured["unknown_exposure_usd"] = str(measured["unknown_exposure_usd"])
    return {"dataset_fingerprint": fingerprint(manifest), "planned_trials": len(plan), "observed_trials": len(found), "missing_trial_ids": sorted(plan.keys() - found.keys()), "incompletely_reviewed_trial_ids": sorted(incomplete_reviews), "original_hard_regression_trial_ids": sorted(regressions), "unknown_exposure_trial_ids": sorted(unknown_trials), "arms": results, "per_arm_usage": arm_usage, "paired_additional_cost_and_active_time": paired, "usage": {key: str(value) if isinstance(value, Decimal) else value for key, value in totals.items()}, "per_trial_limit_violations": sorted(set(over_limit)), "batch_within_declared_limits": batch_within, "phase_8_passed": False, "release_decision_required": "Inspect actual blinded pixels and independent originals; apply the preregistered paired benefit/regression rule and cost/wait criteria. Repeats do not increase the primary denominator. Model agreement or a successful generation never releases automatic improvement."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    result = summarize(manifest, json.loads(args.results.read_text())) if args.results else build_plan(manifest) if args.plan else {"dataset_fingerprint": validate_manifest(manifest), "phase_8_passed": False}
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
