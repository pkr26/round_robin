"""Deterministic partial comparison: no invented preference weights or probabilities."""
from decimal import Decimal, localcontext


def cost_comparison(required_ids, comparisons):
    """Evaluate each option once, then compare exact totals within compatible groups."""
    groups, unresolved, failed = {}, [], []
    for item in comparisons:
        identity = item["option_id"]
        results = item["constraint_results"]
        if any(result["requirement_id"] in required_ids and result["result"] == "fail" and result.get("basis_refs")
               and not result.get("disputed") for result in results):
            failed.append(identity)
            continue
        if (item["total"] is None or item["missing_components"]
                or {result["requirement_id"] for result in results} != required_ids
                or any(result["result"] not in {"pass", "not_applicable"} or result.get("disputed") or not result.get("basis_refs") for result in results)):
            unresolved.append(identity)
            continue
        applicable = frozenset(result["requirement_id"] for result in results if result["result"] == "pass")
        group = (item.get("currency"), item.get("horizon_months"), applicable)
        groups.setdefault(group, []).append((identity, Decimal(item["total"])))
    frontier, dominated = [], []
    for alternatives in groups.values():
        lowest = min(amount for _, amount in alternatives)
        for identity, amount in alternatives:
            (frontier if amount == lowest else dominated).append(identity)
    blocker = None
    if unresolved:
        blocker = "unresolved_options"
    elif len({group[:2] for group in groups}) > 1:
        blocker = "different_cost_basis"
    elif len(groups) > 1:
        blocker = "different_applicability"
    elif failed and not groups:
        blocker = "all_options_failed"
    return {"cost_frontier_option_ids": sorted(frontier), "dominated_option_ids": sorted(dominated),
            "unresolved_option_ids": sorted(unresolved), "failed_option_ids": sorted(failed),
            "tied_lowest_cost_option_ids": sorted(frontier) if not blocker and len(frontier) > 1 else [],
            "cost_comparison_blocker": blocker}


def comparable_cost_leaders(required_ids, comparisons):
    comparison = cost_comparison(required_ids, comparisons)
    return [] if comparison["cost_comparison_blocker"] else comparison["cost_frontier_option_ids"]


def comparison_blocker_note(insights, comparisons):
    blocker = insights.get("cost_comparison_blocker")
    if blocker is None:
        return None
    if blocker == "unresolved_options":
        identities = insights["unresolved_option_ids"]
        message = "Comparable costs or hard-requirement evidence remain unresolved for"
    elif blocker == "all_options_failed":
        identities = insights["failed_option_ids"]
        message = "Every considered option has a documented hard-requirement failure"
    else:
        identities = insights["cost_frontier_option_ids"] + insights["dominated_option_ids"]
        message = ("A common cost ranking is unavailable because different hard requirements apply to these options"
                   if blocker == "different_applicability" else "Cost currency or comparison horizon differs across these options")
    labels = sorted(item["label"] for item in comparisons if item["option_id"] in identities)
    return message + ": " + ", ".join(labels) + "."


def decision_insights(brief, comparisons):
    ranking = cost_comparison({requirement.id for requirement in brief.hard_requirements}, comparisons)
    sensitivity = []
    with localcontext() as context:
        context.prec = 64
        for item in comparisons:
            if item["total"] is None:
                continue
            for requirement in brief.hard_requirements:
                if requirement.metric != "total_cost" or requirement.threshold is None:
                    continue
                total, threshold = Decimal(item["total"]), Decimal(requirement.threshold)
                margin = total - threshold if requirement.operator == "gte" else threshold - total
                if requirement.operator == "eq" and margin > 0:
                    margin = -margin
                sensitivity.append({"option_id": item["option_id"], "requirement_id": requirement.id, "threshold": requirement.threshold,
                    "total": item["total"], "margin": format(margin, "f"), "unit": brief.currency,
                    "scope": f"Signed margin for {requirement.operator} using supplied costs; not a probability or forecast."})
    return {**ranking, "sensitivity": sensitivity, "preference_order": [item.id for item in sorted(brief.preferences, key=lambda item: item.rank)],
        "limitations": ["Cost frontier compares supplied totals only among options with matching assessed hard-requirement results; it is not an overall utility ranking.",
            "Preference ranks are ordinal. No cardinal weights, probability of correctness, or trade-off scores were invented."]}
