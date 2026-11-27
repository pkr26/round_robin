"""Explain the decisive checked cost or feasibility difference at publication."""

from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, localcontext
from functools import lru_cache

from round_robin.reports.questions import _cost_limits, _current_subtotal
from round_robin.research.calculator import CalculationError, break_even, cost_schedule, lifecycle_total
from round_robin.schemas.briefs import DraftBrief, deadline_requirement_description
from round_robin.schemas.evidence import Calculation


def _short(text, limit=150):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _number(value):
    value = format(value, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _cost_fact(row):
    return f"{_short(row['label'])} totals {row['currency']} {row['total']} over {row['horizon_months']} months"


def _deadline_status(brief, rows):
    if not rows or brief.deadline is None:
        return "", []
    # This is the explicit dated obligation constructed by resolved_brief. Its
    # ID may be owner-supplied or suffixed when the usual server ID is taken.
    description = deadline_requirement_description(brief.deadline)
    requirements = {req.id for req in brief.hard_requirements if req.metric == "manual" and req.description == description}
    labels_by_status, refs = {}, []
    for row in rows:
        verdict = next((item for item in row["constraint_results"] if item["requirement_id"] in requirements), {})
        status = verdict.get("result", "unknown").replace("_", " ")
        if verdict.get("disputed"):
            status = "disputed"
        elif not verdict.get("basis_refs"):
            status = "unknown"
        elif verdict["method"] in {"model", "human"}:
            status += " by model assessment" if verdict["method"] == "model" else " by owner attestation"
        labels_by_status.setdefault(status, []).append(_short(row["label"]))
        refs.extend([*verdict.get("basis_refs", []), *verdict.get("counter_basis_refs", [])])
    verdicts = [f"{status} for {', '.join(labels)}" for status, labels in labels_by_status.items()]
    return f" The saved {brief.deadline} deadline assessment is " + " and ".join(verdicts) + ".", refs


def _failed_requirement(brief, row):
    result = min((item for item in row["constraint_results"]
                  if item["result"] == "fail" and item["basis_refs"] and not item.get("disputed")),
                 key=lambda item: item["requirement_id"])
    requirement = next(item for item in brief.hard_requirements if item.id == result["requirement_id"])
    label = _short(row["label"])
    if requirement.metric == "total_cost" and row["total"] is not None:
        target = f"{row['currency']} {requirement.threshold}"
        term = {"lte": "ceiling", "gte": "minimum", "eq": "exact target"}[requirement.operator]
        fact = f"{_cost_fact(row)}, outside the {target} {term}."
        change = f"A revised {row['horizon_months']}-month total for {label} that meets the {target} {term} would clear this cost requirement."
    else:
        description = _short(requirement.description, 230)
        fact = f"The saved assessment marks “{description}” as failed for {label}."
        change = f"Dated evidence that {label} meets “{description}” could change its feasibility assessment."
    return fact, change, result["basis_refs"]


def _pair_costs(brief, first, second, as_of):
    """Describe the saved horizon. Changing the decision needs a checked scenario."""
    options = {option.id: option for option in brief.options}
    a, b = options[first["option_id"]], options[second["option_id"]]
    left, right = _short(a.label), _short(b.label)
    currency, horizon = brief.currency, brief.horizon_months
    facts = f"At {horizon} months, {left} totals {currency} {first['total']} and {right} totals {currency} {second['total']}."
    references = [first["calculation_id"], second["calculation_id"], *dict.fromkeys(
        component.source_ref for option in (a, b) for component in option.cost_components if component.status == "included")]

    with localcontext(Context(prec=64)):
        gap = Decimal(first["total"]) - Decimal(second["total"])
        if gap:
            lower, higher = (left, right) if gap < 0 else (right, left)
            facts += f" {lower} costs {currency} {_number(abs(gap))} less at this horizon."
            change = f"At {horizon} months, {higher} would have to close a {currency} {_number(abs(gap))} gap in the rounded totals to tie {lower}."
        else:
            change = f"The rounded {horizon}-month totals tie. A revised charge that changes one total would break the cost tie."
        action = f"Confirm the quoted one-time and monthly charges for {left} and {right} over your {horizon}-month plan."
        upfront_a, monthly_a = cost_schedule(a, currency, as_of=as_of)
        upfront_b, monthly_b = cost_schedule(b, currency, as_of=as_of)
        if monthly_a == monthly_b:
            facts += f" Both have the same monthly cost of {currency} {_number(monthly_a)}."
            if upfront_a == upfront_b:
                facts += f" Their one-time costs also match at {currency} {_number(upfront_a)}."
                change = f"Under these terms {left} and {right} tie at every horizon where both totals are valid. A difference in their quoted charges or in how they meet your preferences would distinguish them."
            else:
                facts += f" Their one-time costs differ by {currency} {_number(abs(upfront_a - upfront_b))}, so the term alone cannot reverse their cost order."
                action = f"Verify the {currency} {_number(abs(upfront_a - upfront_b))} difference in quoted one-time costs between {left} and {right}."
            return facts, change, action, references

        facts += (f" {left} has {currency} {_number(upfront_a)} in one-time costs and {currency} {_number(monthly_a)} per month."
                  f" {right} has {currency} {_number(upfront_b)} in one-time costs and {currency} {_number(monthly_b)} per month.")
        if (upfront_a - upfront_b) * (monthly_a - monthly_b) > 0:
            lower = left if monthly_a < monthly_b else right
            facts += f" {lower} has both the lower one-time cost and the lower monthly cost."
        return facts, change, action, references


@dataclass
class _Month:
    months: int
    totals: dict[str, Calculation]
    # None means a complete quote could not be totaled at this horizon.
    # An empty tuple means every checked total failed a hard cost requirement.
    leaders: tuple[str, ...] | None


def _checked_crossing(first, second, currency, as_of, previous, boundary, at):
    """Keep an algebraic intersection only if rounded, eligible leaders witness it."""
    if not previous.leaders or not boundary.leaders:
        return None
    try:
        crossing = break_even(first, second, currency, as_of=as_of)
    except CalculationError:
        return None
    point = Decimal(crossing.output)
    if not 1 <= point <= 120:
        return None
    floor = int(point)
    integral = point == floor
    before, after = floor - int(integral), floor + 1
    if not before <= boundary.months <= after:
        return None

    def cheaper_leader(month):
        if not 1 <= month <= 120:
            return None
        scenario = at(month)
        if not scenario.leaders:
            return None
        for key, rival in ((first.id, second.id), (second.id, first.id)):
            if key in scenario.leaders and Decimal(scenario.totals[key].output) < Decimal(scenario.totals[rival].output):
                return scenario, key
        return None

    early, late = cheaper_leader(before), cheaper_leader(after)
    middle = at(floor) if integral else None
    if middle and not {first.id, second.id}.issubset(middle.leaders or ()):
        return None
    if early and late and early[1] != late[1]:
        return crossing, early, late, middle
    if middle and ((before == 0 and late) or (after == 121 and early)):
        return crossing, early, late, middle
    return None


def _horizon_comparison(brief, rows, as_of, chosen):
    """Find the nearest change to the lowest eligible rounded total on the whole-month grid."""
    horizon, currency = brief.horizon_months, brief.currency
    options = {option.id: option for option in brief.options}
    by_id = {row["option_id"]: row for row in rows}
    chosen = chosen if chosen in by_id else None
    ids = sorted(by_id, key=lambda key: (by_id[key]["label"].casefold(), by_id[key]["label"], key))
    cost_rules = [rule for rule in brief.hard_requirements if rule.metric == "total_cost"]
    minimum = max((Decimal(rule.threshold) for rule in cost_rules if rule.operator in {"eq", "gte"}), default=None)
    maximum = min((Decimal(rule.threshold) for rule in cost_rules if rule.operator in {"eq", "lte"}), default=None)

    @lru_cache(maxsize=120)
    def at(month):
        totals, eligible = {}, {}
        for index, key in enumerate(ids):
            identity = f"calc_horizon_{month}_{index + 1}"
            try:
                calc = lifecycle_total(options[key], month, currency, as_of=as_of, calculation_id=identity)
            except ValueError as error:
                reason = str(error) if isinstance(error, CalculationError) else "The total exceeds the calculation bounds."
                calc = Calculation(id=identity, label=f"{_short(by_id[key]['label'])}: {month}-month cost",
                                   operation="lifecycle_total", unit=currency, currency=currency, horizon_months=month,
                                   inputs=[], formula="Unavailable", rounding="No arithmetic result was established",
                                   missing_components=[reason])
            totals[key] = calc
            if calc.output is not None:
                price = Decimal(calc.output)
                if (minimum is None or price >= minimum) and (maximum is None or price <= maximum):
                    eligible[key] = price
        if any(calc.output is None for calc in totals.values()):
            leaders = None
        else:
            lowest = min(eligible.values(), default=None)
            leaders = tuple(key for key, price in eligible.items() if price == lowest)
        return _Month(month, totals, leaders)

    current = at(horizon)
    # Skip changes of horizon for a selection made on other grounds.
    boundary = None
    if not chosen or chosen in (current.leaders or ()):
        for month in sorted(range(1, 121), key=lambda value: (abs(value - horizon), value)):
            scenario = at(month)
            if scenario.leaders != current.leaders:
                boundary = scenario
                break
    by_price = sorted(ids, key=lambda key: Decimal(by_id[key]["total"]))
    first = chosen or next(iter(current.leaders or ()), by_price[0])
    rivals = [key for key in by_price if key != first]
    other_leaders = (boundary.leaders if boundary else ()) or ()
    # At a current tie the comparison needs the winner on each side of the transition.
    if boundary and current.leaders and len(current.leaders) > 1 and other_leaders:
        first = next((key for key in current.leaders if key not in other_leaders), first)
        rivals = [key for key in by_price if key != first]
    second = next((key for key in other_leaders if key != first), rivals[0])
    facts, change, action, refs = _pair_costs(brief, by_id[first], by_id[second], as_of)
    refs.extend(by_id[key]["calculation_id"] for key in ids)
    extra = {}

    def cite(scenario):
        # Establishing a leader (or that every offer fails) requires every
        # compared total at that month. Current-horizon records already exist.
        if scenario.months == horizon:
            return
        for calc in scenario.totals.values():
            if calc.id not in extra:
                refs.append(calc.id)
                extra[calc.id] = calc.model_dump(mode="json")

    def describe(scenario):
        month = scenario.months
        if scenario.leaders is None:
            names = ", ".join(_short(by_id[key]["label"], 60) for key, calc in scenario.totals.items()
                              if calc.output is None)
            return f"At month {month}, a checked total is unavailable for {names}, so the cost ranking is unresolved."
        if not scenario.leaders:
            rules = []
            if minimum is not None:
                rules.append(f"at least {currency} {minimum}")
            if maximum is not None:
                rules.append(f"at most {currency} {maximum}")
            return f"At month {month}, no option meets the required total of {' and '.join(rules)}."
        names = " and ".join(_short(by_id[key]["label"], 60) for key in scenario.leaders)
        total = scenario.totals[scenario.leaders[0]].output
        term = "has the lowest eligible cost" if len(scenario.leaders) == 1 else "tie for the lowest eligible cost"
        return f"At month {month}, {names} {term} at {currency} {total}."

    if not current.leaders or first not in current.leaders or set(current.leaders) - {first, second}:
        facts += " " + describe(current)
    failed_comparator = next((by_id[key] for key in (first, second) if any(
        verdict["result"] == "fail" and verdict["basis_refs"] and not verdict.get("disputed")
        for verdict in by_id[key]["constraint_results"]
    )), None)
    if failed_comparator:
        failure, price_change, failure_refs = _failed_requirement(brief, failed_comparator)
        facts += " " + failure
        change = price_change
        refs.extend(failure_refs)
        action = f"Review the failed cost requirement for {_short(failed_comparator['label'])}."
    if boundary is None:
        return facts, change, action, refs, []
    step = 1 if boundary.months > horizon else -1
    previous = at(boundary.months - step)
    crossover = _checked_crossing(options[first], options[second], currency, as_of, previous, boundary, at)
    if crossover:
        crossing, early, late, middle = crossover
        when = "approximately " + format(Decimal(crossing.output), ".6g") if crossing.flags else _number(Decimal(crossing.output))
        left, right = _short(by_id[first]["label"]), _short(by_id[second]["label"])
        crossing = crossing.model_copy(update={"label": f"Break-even horizon for {left} and {right}"})
        extra[crossing.id] = crossing.model_dump(mode="json")
        refs.append(crossing.id)
        change = f"The break-even is {when} months."
        if early and late:
            before, earlier = early
            after, later = late
            change += (f" {_short(by_id[earlier]['label'])} costs less before that point at month {before.months} and "
                       f"{_short(by_id[later]['label'])} costs less after it at month {after.months}, using the checked whole-month totals.")
        else:
            change += " " + describe(previous) + " " + describe(boundary)
        if middle:
            common = middle.totals[first].output
            facts += f" Both total {currency} {common} at month {middle.months}."
            cite(middle)
            if len(middle.leaders) > 2:
                change += " " + describe(middle)
        for side in (early, late):
            if side:
                state, _ = side
                cite(state)
                if len(state.leaders) > 1:
                    change += " " + describe(state)
        action = f"Confirm how long you expect to use {left} or {right} against their break-even at {when} months."
        return facts, change, action, refs, list(extra.values())

    # A feasibility boundary, a currency-rounding tie or an invalid subtotal can
    # change the decision without a mathematical intersection of the two prices.
    boundary_change = describe(previous) + " " + describe(boundary)
    if len(current.leaders or ()) > 1 and current.leaders == tuple(ids):
        boundary_change = change + " " + boundary_change
    action = f"Confirm whether your planned term falls on the month {previous.months} or month {boundary.months} side of this change."
    if boundary.leaders is None:
        action = f"Confirm the cost terms for month {boundary.months} before extending the comparison to that horizon."
    cite(previous)
    cite(boundary)
    return facts, boundary_change, action, refs, list(extra.values())


def _bounded_tradeoff(brief, comparisons, price_rows, viable, quoted):
    """A strict separation of incomplete quotes can settle their conditional price order."""
    rows = [row for row in comparisons if row["option_id"] in viable]
    if len(rows) < 2:
        return None
    basis = {(row["currency"], row["horizon_months"],
              frozenset(verdict["requirement_id"] for verdict in row["constraint_results"]
                        if verdict["result"] == "not_applicable")) for row in rows}
    if len(basis) != 1:
        return None
    by_upper = sorted(rows, key=lambda row: (viable[row["option_id"]][1], row["label"].casefold(), row["option_id"]))
    winner, *peers = by_upper
    key = winner["option_id"]
    complete_ids = {row["option_id"] for row in price_rows}
    if key in complete_ids and (len(complete_ids) > 1 or all(row["option_id"] in complete_ids for row in rows)):
        return None
    rival = min(peers, key=lambda row: (viable[row["option_id"]][0], row["label"].casefold(), row["option_id"]))
    upper = viable[key][1]
    lower = viable[rival["option_id"]][0]
    if upper >= lower:
        return None

    name, other = _short(winner["label"]), _short(rival["label"])
    currency, horizon = brief.currency, brief.horizon_months
    conditional = quoted[key][1] != upper or quoted[rival["option_id"]][0] != lower
    scope = "Among quotes that meet the hard cost limits, " if conditional else "Under the current quote terms, "
    fact = (f"{scope}{name} costs at most {currency} {upper:f} over {horizon} months. "
            f"{other} costs at least {currency} {lower:f} on that basis.")
    if len(peers) > 1:
        fact += " Every other offer that could qualify has an equal or higher lower bound."
    if len(rows) < len(comparisons):
        fact += " The remaining options cannot meet all hard requirements at this horizon."
    fact += f" If it qualifies, {name} is cheaper than every qualifying alternative at this horizon."
    if key not in complete_ids:
        fact += f" {name} still needs a complete checked quote."
    else:
        fact += f" {name} has a checked total of {currency} {winner['total']}."
    with localcontext(Context(prec=64)):
        difference = lower - upper
    change = (f"For {other} to match {name} at {horizon} months, revised charges would have to close at least "
              f"the {currency} {difference:f} gap between their cost bounds.")
    if brief.hard_requirements:
        change += f" A failed hard requirement could also exclude {name}."
    action = f"Confirm {name}’s quoted terms for {horizon} months against the {currency} {upper:f} upper cost bound."
    refs = [row["calculation_id"] for row in comparisons]
    refs.extend(part.source_ref for option in brief.options if option.id in viable for part in option.cost_components)
    return fact, change, action, refs


def _complete_quote(option, row, as_of):
    return row["total"] is not None and not row["missing_components"] and not any(
        part.quote_valid_until and part.quote_valid_until < as_of for part in option.cost_components
    )


def _current_quote_fact(option, row, as_of):
    if _complete_quote(option, row, as_of):
        return _cost_fact(row) + "."
    name = _short(row["label"])
    if row["total"] is not None:
        return f"The saved {row['horizon_months']}-month total for {name} under the recorded terms is {row['currency']} {row['total']}."
    fact = f"{name} has no complete current total for {row['horizon_months']} months."
    if any(part.status == "included" and part.amount is not None
           and (not part.quote_valid_until or part.quote_valid_until >= as_of) for part in option.cost_components):
        subtotal, _ = _current_subtotal(option, row, as_of)
        if subtotal is not None:
            fact += f" Its current subtotal is {row['currency']} {subtotal:f}."
    return fact


def _over_budget_tradeoff(brief, comparisons, quoted, fixed_failures):
    rules = [rule for rule in brief.hard_requirements if rule.metric == "total_cost"]
    with localcontext(Context(prec=64)):
        minimum, maximum = _cost_limits(rules, brief.currency)
    if not maximum.is_finite():
        return None
    currency, horizon = brief.currency, brief.horizon_months
    raw_min = max((Decimal(rule.threshold) for rule in rules if rule.operator in {"gte", "eq"}), default=Decimal(0))
    raw_max = min(Decimal(rule.threshold) for rule in rules if rule.operator in {"lte", "eq"})
    rounding_note = f" The entered lower bound is {currency} {raw_min:f} before currency rounding." if raw_min != minimum else ""
    if raw_max != maximum:
        rounding_note += f" The entered upper bound is {currency} {raw_max:f} before currency rounding."
    if minimum > maximum:
        fact = (f"The cost requirements have no common total in {currency} minor units. "
                f"The total must reach {currency} {minimum:f} while staying at or below {currency} {maximum:f}.{rounding_note}")
        return (fact, "A revised cost requirement with an attainable total would allow a feasibility comparison.",
                f"Review the cost requirements and reconcile the {currency} {minimum:f} minimum with the {currency} {maximum:f} maximum.", [])
    candidates = [row for row in comparisons if row["option_id"] not in fixed_failures]
    if not candidates or any(quoted[row["option_id"]][0] <= maximum for row in candidates):
        return None
    ordered = sorted(candidates, key=lambda row: (
        quoted[row["option_id"]][0], row["label"].casefold(), row["label"], row["option_id"]))
    closest = ordered[0]
    name, floor = _short(closest["label"]), quoted[closest["option_id"]][0]
    target = f"at most {currency} {maximum:f}"
    limit_fact = f"The cost ceiling is {currency} {maximum:f}."
    if minimum == maximum:
        target = f"exactly {currency} {maximum:f}"
        limit_fact = f"The required total is {target}."
    elif minimum > 0:
        target = f"between {currency} {minimum:f} and {currency} {maximum:f}, inclusive"
        limit_fact = f"The required total is {target}."
    amounts = [f"{_short(row['label'])} costs at least {currency} {quoted[row['option_id']][0]:f}" for row in ordered]
    fact = f"No current quote can meet all hard requirements over {horizon} months. {limit_fact}{rounding_note} " + ". ".join(amounts) + "."
    refs = [row["calculation_id"] for row in ordered]
    for row in comparisons:
        if row["option_id"] in fixed_failures:
            description, basis = fixed_failures[row["option_id"]]
            fact += f" The saved assessment for {_short(row['label'])} documents a failure of “{_short(description)}”."
            refs.extend(basis)
    with localcontext(Context(prec=64)):
        reduction = floor - maximum
    change = (f"{name}’s {horizon}-month total must be {target}. Its current cost floor of {currency} {floor:f} "
              f"requires a reduction of at least {currency} {reduction:f}. Any charges above that floor increase the reduction required.")
    option = next(item for item in brief.options if item.id == closest["option_id"])
    open_charges = sorted({
        part.label for part in option.cost_components if Decimal(part.quantity) and
        (part.status == "excluded" or part.status == "included" and part.amount is None)
    }, key=lambda label: (label.casefold(), label))
    inclusions = "including all charges"
    if open_charges:
        inclusions += " such as " + ", ".join(_short(label, 100) for label in open_charges[:3])
    action = (f"Ask {name} to quote {target} over {horizon} months, {inclusions}. "
              f"The cost requirement calls for a price reduction of at least {currency} {reduction:f}.")
    refs.extend(part.source_ref for option in brief.options if option.id not in fixed_failures for part in option.cost_components)
    return fact, change, action, refs


def explain_tradeoff(brief: DraftBrief, comparisons, insights, *, as_of: date, question, lead_option_id,
                     viable_cost_ranges, quoted_cost_ranges, chosen, lead_cost_gap=None, lead_peer_id=None):
    """Return saved guidance and the calculation records that support it."""

    def result(fact, change, action, refs=(), calculations=None, highlighted=None):
        if highlighted is None:
            highlighted = [row for row in comparisons if row["calculation_id"] in refs]
        deadline, deadline_refs = _deadline_status(brief, highlighted)
        fact += deadline
        refs = [*refs, *deadline_refs]
        if question:
            action = question["question"]
            refs = [*question.get("basis_refs", []), *refs]
            if question["affected_conclusion"] != change and question["affected_conclusion"] not in fact:
                change += " " + question["affected_conclusion"]
        calc_ids = {row["calculation_id"] for row in comparisons}
        calc_ids.update(calc["id"] for calc in calculations or ())
        refs = sorted(dict.fromkeys(refs), key=lambda ref: ref not in calc_ids)
        return {"tradeoff": _short(fact, 4000), "what_would_change_answer": _short(change, 2000), "next_action": action,
                "tradeoff_basis_refs": refs[:64]}, calculations or []

    if not comparisons:
        action = ("Set the number of months over which to compare the options." if brief.horizon_months is None else
                  "Supply comparable options and current cost inputs for the saved horizon.")
        return result("No checked option totals are available for this decision.", action, action)

    failed_ids = set(insights["failed_option_ids"])
    contenders = [item for item in comparisons if item["option_id"] not in failed_ids]
    failed = sorted((item for item in comparisons if item["option_id"] in failed_ids), key=lambda item: item["option_id"])
    option_by_id = {option.id: option for option in brief.options}
    if question and lead_option_id is None:
        return result(question["affected_conclusion"],
                      "After the owner resolves the conflicting limits, a dated supplier commitment can establish eligibility.",
                      question["question"], question["basis_refs"])
    if question and lead_cost_gap:
        by_id = {row["option_id"]: row for row in comparisons}
        name = _short(option_by_id[lead_option_id].label)
        subject = _short(lead_cost_gap.subject, 240)
        if lead_cost_gap.expiry:
            fact = f"The quote for {name}’s {subject} expired on {lead_cost_gap.expiry}."
        elif subject:
            fact = f"The amount to include for {name}’s {subject} is unresolved."
        else:
            fact = f"{name} needs corrected pricing to establish an all-in cost."
        highlighted = [by_id[key] for key in (lead_option_id, lead_peer_id) if key is not None]
        for row in highlighted:
            fact += " " + _current_quote_fact(option_by_id[row["option_id"]], row, as_of)
        return result(fact, question["affected_conclusion"], question["question"], question["basis_refs"], highlighted=highlighted)

    # An option that fails only on price today can qualify when the term changes.
    noncost = {rule.id: rule for rule in brief.hard_requirements if rule.metric != "total_cost"}
    fixed_failures = {}
    for row in failed:
        for verdict in sorted(row["constraint_results"], key=lambda item: item["requirement_id"]):
            rule = noncost.get(verdict["requirement_id"])
            if rule and verdict["result"] == "fail" and verdict["basis_refs"] and not verdict.get("disputed"):
                fixed_failures[row["option_id"]] = (rule.description, verdict["basis_refs"])
                break
    complete = [row for row in comparisons if _complete_quote(option_by_id[row["option_id"]], row, as_of)]
    price_rows = [row for row in complete if row["option_id"] not in fixed_failures]
    budget = None if viable_cost_ranges else _over_budget_tradeoff(brief, comparisons, quoted_cost_ranges, fixed_failures)
    if budget and len(price_rows) < 2:
        return result(*budget)
    if not question:
        bounds = _bounded_tradeoff(brief, comparisons, price_rows, viable_cost_ranges, quoted_cost_ranges)
        if bounds:
            return result(*bounds)

    if len(price_rows) < 2:
        if not contenders and failed:
            fact, change, refs = _failed_requirement(brief, failed[0])
            return result(f"Every considered option has a documented hard-requirement failure. {fact}", change,
                          f"Review the failed requirement for {_short(failed[0]['label'])} before choosing.", refs,
                          highlighted=failed[:1])
        if not price_rows:
            if not viable_cost_ranges:
                action = "Review the hard requirements or obtain revised terms that can meet them."
                return result("No option can meet all hard requirements within the bounds of its current quote.",
                              action, action, [row["calculation_id"] for row in comparisons])
            action = "Confirm a complete current quote for an option that can meet the hard requirements."
            return result("A cost comparison needs a complete current quote for a feasible option.", action, action)
        row = price_rows[0]
        fact = _cost_fact(row) + "."
        refs = [row["calculation_id"]]
        if row["option_id"] in failed_ids:
            reason, change, basis = _failed_requirement(brief, row)
            if not viable_cost_ranges:
                reason += f" No current quote can meet all hard requirements over {row['horizon_months']} months."
            return result(reason, change, f"Obtain revised terms that meet the cost requirement for {_short(row['label'])}.",
                          [*refs, *basis])
        other_failure = next((item for item in failed if item is not row), None)
        if other_failure:
            reason, change, basis = _failed_requirement(brief, other_failure)
            return result(fact + " " + reason, change, f"Review the failed requirement for {_short(other_failure['label'])}.",
                          [*refs, *basis], highlighted=[row, other_failure])
        action = f"Confirm the quoted {row['horizon_months']}-month total for {_short(row['label'])}."
        return result(fact, "Another complete, eligible quote could establish a cost difference.", action, refs)

    unresolved = set(insights["unresolved_option_ids"])
    unresolved_baseline = next((row for row in price_rows if row["role"] == "status_quo"
                                and row["option_id"] in unresolved), None)
    pair_pool = price_rows
    if unresolved_baseline and len(price_rows) > 2 and chosen != unresolved_baseline["option_id"]:
        pair_pool = [row for row in price_rows if row is not unresolved_baseline]
    first = pair_pool[0]
    for other in pair_pool[1:]:
        if (other["currency"], other["horizon_months"]) != (first["currency"], first["horizon_months"]):
            return result(f"{_cost_fact(first)}. {_cost_fact(other)}. The comparison bases differ.",
                          "A common currency and horizon would allow their totals to be compared.",
                          "Obtain both totals on the same basis.", [first["calculation_id"], other["calculation_id"]])
        for requirement in brief.hard_requirements:
            verdicts = [next((item for item in row["constraint_results"] if item["requirement_id"] == requirement.id), {})
                        for row in (first, other)]
            statuses = [verdict.get("result", "unknown") for verdict in verdicts]
            if "not_applicable" in statuses and statuses[0] != statuses[1]:
                name = _short(requirement.description, 230)
                fact = (f"The options have different assessments for “{name}”. {_short(first['label'])} is assessed as "
                        f"{statuses[0].replace('_', ' ')} and {_short(other['label'])} as {statuses[1].replace('_', ' ')}.")
                action = f"Confirm where the requirement “{name}” applies before ranking these options."
                return result(fact, action, action, [ref for verdict in verdicts for ref in verdict.get("basis_refs", [])],
                              highlighted=[first, other])

    facts, change, action, refs, calculations = _horizon_comparison(brief, pair_pool, as_of, chosen)
    if budget and not calculations:
        # No other supported term produced a feasibility boundary for these offers.
        return result(*budget)
    if len(pair_pool) < len(comparisons) - len(fixed_failures):
        names = " and ".join(_short(row["label"], 55) for row in pair_pool)
        scope = f"The comparison covers the complete current quotes for {names}."
        facts += " " + scope
        change = scope + " " + change
    if any(row["option_id"] in unresolved for row in pair_pool):
        condition = "These cost comparisons depend on the unresolved hard requirements being met."
        facts += " " + condition
        change += " " + condition
    if unresolved_baseline and unresolved_baseline not in pair_pool:
        pending = {verdict["requirement_id"] for verdict in unresolved_baseline["constraint_results"]
                   if verdict["result"] == "unknown" or verdict.get("disputed") or not verdict["basis_refs"]}
        obligation = next((_short(rule.description) for rule in brief.hard_requirements if rule.id in pending), None)
        facts += f" The status quo, {_short(unresolved_baseline['label'])}, totals {brief.currency} {unresolved_baseline['total']}."
        if obligation:
            facts += f" Its eligibility depends on the unresolved requirement “{obligation.rstrip('.')}”."
        refs.append(unresolved_baseline["calculation_id"])
    return result(facts, change, action, refs, calculations)
