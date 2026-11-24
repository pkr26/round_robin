"""Paired, task-cluster-aware analysis of retained blinded human grades.

This is descriptive evaluation tooling, not an automatic release decision. All
outputs remain in the planned primary denominator. The seed and resampling count
belong in the preregistered analysis plan, not selected after seeing outcomes.
"""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
from decimal import Context, Decimal, InvalidOperation, localcontext

from .evaluation import ManualGrade, TrialOutput, summarize_workflow, fingerprint, deadline_attainment


def exact_paired_pvalue(wins, losses):
    if any(type(value) is not int or value < 0 for value in (wins, losses)):
        raise ValueError("Discordant paired counts must be nonnegative integers")
    n = wins + losses
    if n > 10000:
        raise ValueError("Paired analysis exceeds its bounded case capacity")
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2**n) if n else 1.0


def zero_failure_upper_bound(n, *, alpha=.05):
    if type(n) is not int or n <= 0 or not 0 < alpha < 1:
        raise ValueError("A positive independent sample size and valid alpha are required")
    return -math.expm1(math.log(alpha) / n)


def _interval(rows, values, *, seed, resamples):
    groups = defaultdict(list)
    for row, value in zip(rows, values, strict=True):
        groups[row.get("cluster_id") or row["task_id"]].append(value)
    clusters = list(groups.values())
    rng, samples = random.Random(seed), []
    for _ in range(resamples):
        draw = [value for group in rng.choices(clusters, k=len(clusters)) for value in group]
        samples.append(sum(draw) / len(draw))
    samples.sort()
    return {"lower": samples[math.floor(.025 * (resamples - 1))], "upper": samples[math.ceil(.975 * (resamples - 1))],
            "confidence": .95, "method": "paired_task_cluster_percentile_bootstrap", "clusters": len(clusters), "resamples": resamples,
            "seed": seed, "limitations": ["Small or unrepresentative task clusters can give unreliable precision; this interval is not a guarantee of generalization."]}


def _performance(measurements, outputs, arms, dataset_hash):
    found = {row.trial_id: row for row in outputs}
    records, by_arm = {}, {arm: [] for arm in arms}
    for row in measurements:
        output = found.get(row.get("trial_id"))
        if output is None or row["trial_id"] in records or row.get("configuration_hash") != output.configuration_fingerprint or row.get("dataset_fingerprint") != dataset_hash or row.get("status") != output.status or f'.{row.get("workflow")}.r' not in row["trial_id"]:
            raise ValueError("Performance observations must bind each retained immutable output exactly once")
        if row.get("workflow") not in arms:
            continue
        for key in ("reported_usd", "unknown_exposure_usd", "conservative_actual_exposure_usd"):
            value = row.get(key)
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("Performance exposure must be finite bounded Decimal text")
            try:
                amount = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("Performance exposure must be Decimal text") from exc
            if not amount.is_finite() or amount < 0 or amount > Decimal("1e30") or amount.as_tuple().exponent < -64:
                raise ValueError("Performance exposure exceeds exact supported bounds")
        with localcontext(Context(prec=128)):
            if Decimal(row["reported_usd"]) + Decimal(row["unknown_exposure_usd"]) != Decimal(row["conservative_actual_exposure_usd"]):
                raise ValueError("Reported cost plus unresolved exposure must equal conservative exposure")
        records[row["trial_id"]] = row
        by_arm[row["workflow"]].append(row)
    result = {}
    for arm, rows in by_arm.items():
        with localcontext(Context(prec=128)):
            result[arm] = {"observed_trials": len(rows), "all_retained_outputs_measured": all(row.trial_id in records for row in outputs if f".{arm}.r" in row.trial_id),
                "reported_usd": str(sum((Decimal(row["reported_usd"]) for row in rows), Decimal(0))),
                "conservative_actual_exposure_usd": str(sum((Decimal(row["conservative_actual_exposure_usd"]) for row in rows), Decimal(0))),
                "unknown_exposure_usd": str(sum((Decimal(row["unknown_exposure_usd"]) for row in rows), Decimal(0))),
                "admitted_trial_timing": deadline_attainment(rows)}
    return {"arms": result, "scope": "Observed retained trials, including repeats; inspect missing planned outputs in quality summaries. Unknown admission is not invented."}


def compare_workflows(dataset, outputs, grades, mapping, *, baseline="single_provider", candidate="bounded_debate", seed, resamples=5000, measurements=None):
    if baseline == candidate or not isinstance(seed, str) or not seed.strip() or type(resamples) is not int or not 1000 <= resamples <= 20000:
        raise ValueError("Declare distinct arms, a recorded seed and 1000–20000 resamples")
    outputs, grades = list(outputs), list(grades)
    summaries = {arm: summarize_workflow(dataset, arm, outputs, grades, mapping) for arm in (baseline, candidate)}
    tasks = dataset["tasks"]
    useful = {arm: set(row["useful_task_ids"]) for arm, row in summaries.items()}
    critical = {arm: set(row["original_critical_defect_task_ids"]) for arm, row in summaries.items()}
    differences = [int(row["task_id"] in useful[candidate]) - int(row["task_id"] in useful[baseline]) for row in tasks]
    wins, losses = differences.count(1), differences.count(-1)
    interval = _interval(tasks, differences, seed=seed, resamples=resamples)
    stratum = {}
    for row, delta in zip(tasks, differences, strict=True):
        group = stratum.setdefault(row["stratum"], {"tasks": 0, "candidate_wins": 0, "baseline_wins": 0, "ties": 0})
        group["tasks"] += 1
        group["candidate_wins"] += delta == 1
        group["baseline_wins"] += delta == -1
        group["ties"] += delta == 0
    critical_difference = None
    zero_defect_bounds = None
    if all(summary["primary_review_complete"] for summary in summaries.values()):
        values = [int(row["task_id"] in critical[candidate]) - int(row["task_id"] in critical[baseline]) for row in tasks]
        critical_difference = {"estimate": sum(values) / len(values), "interval": _interval(tasks, values, seed=seed + ":critical", resamples=resamples), "label": "difference_in_original_human_critical_defect_signals"}
        if interval["clusters"] == len(tasks):
            zero_defect_bounds = {arm: {"observed_critical_cases": len(critical[arm]),
                "one_sided_95_upper_rate_if_zero": zero_failure_upper_bound(len(tasks)) if not critical[arm] else None,
                "assumption": "Independent representative cases; zero observed defects do not imply zero risk."} for arm in (baseline, candidate)}
    original_by_packet = defaultdict(list)
    for grade in grades:
        if not grade.adjudication_of:
            original_by_packet[grade.packet_token].append(grade)
    agreement = {"paired_original_reviews": 0, "usefulness_exact_agreements": 0, "critical_defect_agreements": 0}
    for records in original_by_packet.values():
        for index, first in enumerate(records):
            for second in records[index + 1:]:
                agreement["paired_original_reviews"] += 1
                agreement["usefulness_exact_agreements"] += first.usefulness == second.usefulness
                agreement["critical_defect_agreements"] += first.hidden_critical_defect == second.hidden_critical_defect
    return {"schema_version": "paired-human-analysis-1", "dataset_fingerprint": fingerprint(dataset), "baseline": baseline, "candidate": candidate,
            "primary_denominator": len(tasks), "candidate_wins": wins, "baseline_wins": losses, "ties": len(tasks) - wins - losses,
            "usefulness_rate_difference": {"estimate": sum(differences) / len(tasks), "interval": interval},
            "exact_mcnemar_pvalue": exact_paired_pvalue(wins, losses) if interval["clusters"] == len(tasks) else None,
            "critical_defect_difference": critical_difference, "zero_defect_bounds": zero_defect_bounds, "per_stratum": stratum, "original_reviewer_agreement": agreement,
            "performance": _performance(measurements, outputs, (baseline, candidate), fingerprint(dataset)) if measurements is not None else None,
            "arms": summaries, "phase_passed": False,
            "limitations": ["Missing, failed or insufficiently reviewed primary results count as not useful; inspect arm completeness.",
                            "Critical-defect differences are unavailable until both primary review sets are complete; missing review is not evidence of safety.",
                            "Repeated executions are retained in arm diagnostics, not added to independent primary sample size.",
                            "Reviewer agreement does not establish truth or reviewer independence.",
                            "Preregister effect thresholds, cost/latency constraints and multiplicity handling; no numerical result automatically releases a protocol."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset", "outputs", "grades", "mapping"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--baseline", default="single_provider")
    parser.add_argument("--candidate", default="bounded_debate")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--resamples", type=int, default=5000)
    parser.add_argument("--measurements", type=Path)
    args = parser.parse_args()
    dataset, output_rows, grade_rows, mapping = [json.loads(getattr(args, name).read_text()) for name in ("dataset", "outputs", "grades", "mapping")]
    print(json.dumps(compare_workflows(dataset, [TrialOutput(**row) for row in output_rows], [ManualGrade(**row) for row in grade_rows], mapping,
        baseline=args.baseline, candidate=args.candidate, seed=args.seed, resamples=args.resamples,
        measurements=json.loads(args.measurements.read_text()) if args.measurements else None), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
