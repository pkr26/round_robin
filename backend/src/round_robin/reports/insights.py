"""Deterministic partial comparison: no invented preference weights or probabilities."""
from decimal import Decimal, localcontext


def decision_insights(brief, comparisons):
    eligible = [item for item in comparisons if item["total"] is not None and not item["missing_components"] and
        len(item["constraint_results"]) == len(brief.hard_requirements) and
        all(result["result"] in {"pass", "not_applicable"} and not result.get("disputed", False) for result in item["constraint_results"])]
    eligible_ids = {item["option_id"] for item in eligible}
    # Compare cost only among options with identical assessed applicability.
    # This frontier deliberately does not claim to rank unmeasured preferences.
    dominated = set()
    for item in eligible:
        signature = sorted((entry["requirement_id"], entry["result"]) for entry in item["constraint_results"])
        if any(other["option_id"] != item["option_id"] and sorted((entry["requirement_id"], entry["result"]) for entry in other["constraint_results"]) == signature and Decimal(other["total"]) < Decimal(item["total"]) for other in eligible):
            dominated.add(item["option_id"])
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
    return {"cost_frontier_option_ids": sorted(eligible_ids - dominated), "dominated_option_ids": sorted(dominated),
        "unresolved_option_ids": sorted(item["option_id"] for item in comparisons if item["option_id"] not in eligible_ids),
        "sensitivity": sensitivity, "preference_order": [item.id for item in sorted(brief.preferences, key=lambda item: item.rank)],
        "limitations": ["Cost frontier compares supplied totals only among options with matching assessed hard-requirement results; it is not an overall utility ranking.",
            "Preference ranks are ordinal. No cardinal weights, probability of correctness, or trade-off scores were invented."]}
