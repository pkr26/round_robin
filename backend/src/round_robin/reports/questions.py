"""Rank requests by the decision an answer can change at the assessed horizon."""

from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext

from round_robin.research.calculator import CalculationError, lifecycle_total
from round_robin.research.currencies import minor_units
from round_robin.schemas.common import checked_decimal


STANDARD_COST_KINDS = ("upfront", "installation", "recurring", "maintenance", "tax_delivery")
_INFINITY = Decimal("Infinity")
_ZERO = Decimal(0)
_RELATION = {"lte": "at most", "gte": "at least", "eq": "exactly"}


def _excerpt(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass(frozen=True)
class CostGap:
    question: str
    detail: str
    source_refs: tuple[str, ...]
    # The direction of a missing contribution relative to other current inputs.
    # None covers a renewal of mixed terms or a calculation that needs repair.
    direction: int | None
    specificity: int
    units: Decimal | None = None
    frequency: str | None = None
    subject: str = ""
    expiry: str = ""
    line_label: str = ""


@dataclass(frozen=True)
class QuestionWorklist:
    requests: list[dict]
    lead_question: dict | None
    lead_option_id: str | None
    # A feasible interval is the quoted interval intersected with the hard
    # cost limits. An option with an established failure has no viable entry.
    viable_cost_ranges: dict[str, tuple[Decimal, Decimal]]
    quoted_cost_ranges: dict[str, tuple[Decimal, Decimal]]
    lead_cost_gap: CostGap | None
    lead_peer_id: str | None


@dataclass(frozen=True)
class _Quote:
    gaps: list[CostGap]
    subtotal: Decimal | None
    exact: bool
    lower: Decimal
    upper: Decimal


def _cost_gaps(option, row, as_of):
    gaps = []
    expired_groups = {}
    labels = [component.label.casefold() for component in option.cost_components]
    for component in option.cost_components:
        direction = -1 if component.kind == "discount" else 1
        if Decimal(component.quantity) == 0:
            direction = 0
        subject = component.label
        if labels.count(subject.casefold()) > 1:
            subject += f" (line {component.id})"
        units = Decimal(component.quantity) * (row["horizon_months"] if component.frequency == "monthly" else 1)
        if component.quote_valid_until and component.quote_valid_until < as_of:
            key = (component.source_ref, component.quote_valid_until)
            expired_groups.setdefault(key, []).append((component, direction, subject, units))
            continue
        frequency = "monthly" if component.frequency == "monthly" else "one-time"
        if component.status == "excluded":
            question = f"What amount, if any, applies to {subject} for {option.label}? The quote excludes it."
            detail = "Confirm the applicable amount or document why this excluded entry does not apply."
        elif component.status == "included" and component.amount is None:
            question = f"What is the {subject} amount for {option.label}?"
            detail = f"The entered quote records this amount as unknown. {component.unknown_reason}"
        else:
            continue
        detail += f" Confirm the {frequency} amount in {component.currency} and quantity {component.quantity}."
        gaps.append(CostGap(question, detail, (component.source_ref,), direction, 0, units, component.frequency,
                            subject=subject, line_label=component.label))

    for (source_ref, expiry), entries in expired_groups.items():
        names = ", ".join(sorted(name for _, _, name, _ in entries))
        priced = [entry for entry in entries if entry[1]]
        subject = ", ".join(sorted(entry[2] for entry in priced)) or names
        directions = {entry[1] for entry in priced}
        direction = next(iter(directions), 0) if len(directions) <= 1 else None
        question = f"Can {option.label} renew the quote for {_excerpt(subject, 620)} that expired on {expiry}?"
        detail = f"Supply current amounts, exclusions and applicability for {names}, with a new validity date."
        ancillary = all(entry[0].status == "not_applicable" for entry in entries)
        single = priced[0] if len(priced) == 1 else None
        gaps.append(CostGap(question, _excerpt(detail, 1800), (source_ref,), direction, int(ancillary),
                            single[3] if single else None, single[0].frequency if single else None,
                            subject=subject, expiry=str(expiry), line_label=single[0].label if single else ""))

    if row["known_subtotal"] is None:
        question = f"Can {option.label} provide corrected cost inputs for a total in {row['currency']}?"
        detail = "The lifecycle calculation could not establish a subtotal. Check currencies, units, quantities and discounts."
        refs = tuple(sorted({part.source_ref for part in option.cost_components if part.status == "included"}))
        gaps.append(CostGap(question, detail, refs, None, 0))

    present = {component.kind for component in option.cost_components}
    for category in STANDARD_COST_KINDS:
        if category not in present:
            name = category.replace("_", " ")
            question = f"Does {name} apply to {option.label}, and what is the amount?"
            detail = f"The saved quote has no {name} entry. Give the charge or explicitly confirm that it does not apply."
            gaps.append(CostGap(question, detail, (), 1, 1, subject=name))
    if not gaps and row["missing_components"]:
        question = f"Can {option.label} confirm the complete cost, including an explicit zero if applicable?"
        gaps.append(CostGap(question, "Provide a current itemized amount or confirm zero cost in writing.", (), None, 0))
    return gaps


def _current_subtotal(option, row, as_of):
    if row["known_subtotal"] is None:
        return None, False
    current = [part for part in option.cost_components if part.quote_valid_until is None or part.quote_valid_until >= as_of]
    # A historical lifecycle subtotal includes expired known amounts. Remove
    # them before deriving a cost bound. Keep the rounding flag so an exact
    # unit price is quoted only when the subtotal retained all input precision.
    try:
        calc = lifecycle_total(option.model_copy(update={"cost_components": current}), row["horizon_months"],
                               row["currency"], calculation_id=row["calculation_id"], as_of=as_of)
        return Decimal(calc.known_subtotal), not calc.flags
    except (CalculationError, ValueError):
        return None, False


def _cost_limits(requirements, currency):
    lower, upper = _ZERO, _INFINITY
    try:
        unit = Decimal(1).scaleb(-minor_units(currency))
    except ValueError:
        return lower, upper
    for requirement in requirements:
        threshold = Decimal(requirement.threshold)
        if requirement.operator in {"gte", "eq"}:
            lower = max(lower, threshold.quantize(unit, rounding=ROUND_CEILING))
        if requirement.operator in {"lte", "eq"}:
            upper = min(upper, threshold.quantize(unit, rounding=ROUND_FLOOR))
    return lower, upper


def _can_cross(requirement, lower, upper):
    threshold = Decimal(requirement.threshold)
    if requirement.operator == "lte":
        return lower <= threshold < upper
    if requirement.operator == "gte":
        return lower < threshold <= upper
    return lower <= threshold <= upper and lower != upper


def _range(subtotal, gaps):
    if subtotal is None:
        return _ZERO, _INFINITY
    with localcontext() as context:
        context.prec = 64
        lower = upper = subtotal
        for gap in gaps:
            maximum = _INFINITY if gap.units is None else Decimal("1e12") * gap.units
            if gap.direction in {None, -1}:
                lower = max(_ZERO, lower - maximum)
            if gap.direction in {None, 1}:
                upper += maximum
        return lower, upper


def _spans_price(lower, upper, peer_cost):
    return peer_cost is not None and lower <= peer_cost <= upper


def _unit_price(gap, difference):
    """Return an exactly representable quote amount that would match the peer."""
    if not gap.units or not difference:
        return None
    with localcontext() as context:
        context.prec = 64
        context.clear_flags()
        amount = (abs(difference) / gap.units).normalize()
        if context.flags[Inexact]:
            return None
        try:
            return checked_decimal(format(amount, "f"), maximum="1000000000000", places=6)
        except ValueError:
            return None


def _match_note(gap, peer, peer_quote, subtotal, exact, other_entries, combined_count, option_label, interval):
    currency = peer["currency"]
    peer_label = _excerpt(peer["label"], 55)
    price = peer_quote.subtotal
    reference = (f"{peer_label}'s current subtotal of {currency} {price:f}" if peer_quote.gaps else
                 f"{peer_label} at {currency} {price:f}")
    with localcontext() as context:
        context.prec = 64
        difference = price - subtotal
    can_cost_less, can_cost_more = interval[0] < price, interval[1] > price
    if can_cost_less and (difference <= 0 or not can_cost_more):
        boundary = f" An eligible total that rounds lower favors {option_label} on cost."
    elif can_cost_more:
        boundary = f" An eligible total that rounds higher favors {peer_label} on cost."
    else:
        boundary = " The only eligible total would tie on cost."
    if not difference:
        return f"Its subtotal already matches {reference}." + boundary
    exact = exact and peer_quote.exact
    unit_price = _unit_price(gap, difference) if exact and gap.units else None
    if not exact or (not combined_count and gap.units is not None and unit_price is None):
        relation = "above" if difference > 0 else "below"
        price_kind = "current subtotal" if peer_quote.gaps else "checked total"
        return (f"{peer_label}'s {price_kind} is {currency} {price:f}, {currency} {difference.copy_abs():f} "
                f"{relation} the rounded subtotal." + boundary)
    change = "increase" if difference > 0 else "decrease"
    per_unit = ""
    if not combined_count and unit_price and gap.units != 1:
        period = " per month" if gap.frequency == "monthly" else ""
        per_unit = f" ({currency} {unit_price} per unit{period})"
    prefix = ("If other open entries on both offers net to zero, a" if peer_quote.gaps else
              "If other open entries net to zero, a" if other_entries else "A")
    if combined_count:
        prefix += " combined"
        subject = f" across the {combined_count} {_excerpt(gap.line_label, 55)} lines"
    else:
        subject = f" for {_excerpt(gap.subject, 90)}" if gap.subject else ""
    return (f"{prefix} {currency} {difference.copy_abs():f} {change}{subject} over the horizon{per_unit} "
            f"would match {reference}." + boundary)


def _applicability(row):
    return {entry["requirement_id"]: entry["result"] for entry in row["constraint_results"]
            if entry["result"] in {"pass", "not_applicable"} and entry["basis_refs"] and not entry.get("disputed")}


def _comparable_peers(row, peers, noncost_requirements):
    applies = _applicability(row)
    candidates = []
    for other in peers:
        if (other["option_id"] == row["option_id"] or other["currency"] != row["currency"]
                or other["horizon_months"] != row["horizon_months"]):
            continue
        if any(key in applies and applies[key] != value for key, value in _applicability(other).items()):
            continue
        candidates.append(other)
    # A baseline with unresolved charges can still bound the comparison. When
    # its ability to meet a noncost requirement is unknown, compare purchases
    # with each other before relying on that baseline.
    same_scope = [other for other in candidates if other["role"] != "status_quo"
                  or noncost_requirements.issubset(_applicability(other))]
    return same_scope or candidates


def _delivery_conflicts(requirements, comparisons, affordable, fixed_failures):
    """A supplier measurement cannot resolve an empty interval in the brief."""
    delivery = sorted((req for req in requirements if req.metric == "lead_time_days"), key=lambda req: req.id)
    if len(delivery) < 2:
        return [], [], set()
    conflicts = {}
    blocked_options = set()
    actionable = set()
    for row in comparisons:
        results = {entry["requirement_id"]: entry for entry in row["constraint_results"]}
        verdicts = _applicability(row)
        applicable = [req for req in delivery if verdicts.get(req.id) != "not_applicable"]
        minimum = max((req for req in applicable if req.operator in {"eq", "gte"}),
                      key=lambda req: Decimal(req.threshold), default=None)
        maximum = min((req for req in applicable if req.operator in {"eq", "lte"}),
                      key=lambda req: Decimal(req.threshold), default=None)
        if not minimum or not maximum or Decimal(minimum.threshold) <= Decimal(maximum.threshold):
            continue
        blocked_options.add(row["option_id"])
        key = (minimum.id, maximum.id)
        if row["option_id"] in affordable and fixed_failures[row["option_id"]].issubset(key):
            actionable.add(key)
        if key not in conflicts:
            low = f"{_RELATION[minimum.operator]} {minimum.threshold} days"
            high = f"{_RELATION[maximum.operator]} {maximum.threshold} days"
            names = " and ".join(dict.fromkeys(f"“{req.description}”" for req in (minimum, maximum)))
            question = f"For {names}, should the decision owner revise the limit of {low} or {high}?"
            effect = f"No single lead time can satisfy both {low} and {high}."
            conflicts[key] = (question, effect, set(), set())
        _, _, limits, refs = conflicts[key]
        for req in applicable:
            limits.add(f"{req.id} ({_RELATION[req.operator]} {req.threshold} days)")
            verdict = results.get(req.id, {})
            refs.update(ref for field in ("basis_refs", "counter_basis_refs") for ref in verdict.get(field, []))
    leading, remaining = [], []
    for key, (question, effect, limits, refs) in sorted(conflicts.items(), key=lambda item: (item[1][0], item[0])):
        detail = ("If the limits describe different milestones, identify the start and end of each measurement. "
                  "For a shared delivery window, revise the conflicting limits in the brief. "
                  "Applicable requirements include " + ", ".join(sorted(limits)) + ".")
        request = {"question": question, "affected_conclusion": effect,
                   "verification_method": "Have the decision owner confirm the intended delivery window.",
                   "request_text": _excerpt(f"{question} {detail}", 4000), "responsible_person": "Decision owner",
                   "basis_refs": sorted(refs)[:64]}
        (leading if key in actionable else remaining).append(request)
    return leading, remaining, blocked_options


def ranked_questions(brief, comparisons, insights, *, as_of, chosen):
    """Return all distinct requests in deterministic order, before the report's display cap."""
    return rank_question_worklist(brief, comparisons, insights, as_of=as_of, chosen=chosen).requests


def rank_question_worklist(brief, comparisons, insights, *, as_of, chosen):
    """Keep outstanding requests visible even when their answers cannot affect the decision."""
    # Quantity scales, minor-unit thresholds and cost gaps use the same
    # precision as the saved lifecycle calculation, across process contexts.
    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)):
        return _ranked_questions(brief, comparisons, insights, as_of, chosen)


def _ranked_questions(brief, comparisons, insights, as_of, chosen):
    assessed = set(insights["cost_frontier_option_ids"]) | set(insights["dominated_option_ids"])
    options = {option.id: option for option in brief.options}
    cost_requirements = [req for req in brief.hard_requirements if req.metric == "total_cost"]
    noncost_requirements = {req.id for req in brief.hard_requirements if req.metric != "total_cost"}
    cost_min, cost_max = _cost_limits(cost_requirements, brief.currency)
    quotes = {}
    viable = {}
    affordable = set()
    fixed_failures = {}
    for row in comparisons:
        identity = row["option_id"]
        gaps = _cost_gaps(options[identity], row, as_of)
        subtotal, exact = _current_subtotal(options[identity], row, as_of)
        lower, upper = _range(subtotal, gaps)
        quotes[identity] = _Quote(gaps, subtotal, exact, lower, upper)
        interval = max(lower, cost_min), min(upper, cost_max)
        # An expired applicability statement can make a deterministic cost
        # verdict provisional, even when the saved calculation has a total.
        # A separately evidenced failure still rules the option out.
        fixed_failures[identity] = {
            verdict["requirement_id"] for verdict in row["constraint_results"]
            if verdict["result"] == "fail" and verdict["basis_refs"] and not verdict.get("disputed")
            and (verdict["requirement_id"] in noncost_requirements or not gaps
                 or verdict.get("method") != "deterministic_calculation")
        }
        if interval[0] <= interval[1]:
            affordable.add(identity)
            if not fixed_failures[identity]:
                viable[identity] = interval
    owner_requests, deferred_owner_requests, conflicting_delivery = _delivery_conflicts(
        brief.hard_requirements, comparisons, affordable, fixed_failures)
    for identity in conflicting_delivery:
        viable.pop(identity, None)
    peers = [row for row in comparisons if row["option_id"] in viable]
    ranked = []

    for row in comparisons:
        identity = row["option_id"]
        option = options[identity]
        label = _excerpt(option.label, 55)
        currency = row["currency"]
        quote = quotes[identity]
        gaps, subtotal = quote.gaps, quote.subtotal
        current_amount_refs = list(dict.fromkeys(
            part.source_ref for part in option.cost_components
            if part.amount is not None and part.status == "included"
            and (part.quote_valid_until is None or part.quote_valid_until >= as_of)
        ))
        lower, upper = quote.lower, quote.upper
        viable_min, viable_max = max(lower, cost_min), min(upper, cost_max)
        feasible = identity in viable
        candidates = _comparable_peers(row, peers, noncost_requirements)
        # The cheapest competitor can range between the minima of the peer
        # lower and upper bounds. A surcharge above that entire range cannot
        # change the leader, even if every quote still has an open entry.
        peer_floor = min((viable[other["option_id"]][0] for other in candidates), default=_INFINITY)
        peer_ceiling = min((viable[other["option_id"]][1] for other in candidates), default=_INFINITY)
        contender = feasible and viable_min <= peer_ceiling
        order_can_change = (bool(candidates) and viable_min < viable_max and
                            max(viable_min, peer_floor) <= min(viable_max, peer_ceiling))
        # A quoted subtotal gives a specific comparison if the other open
        # amounts net to zero. Never use an unaffordable or dominated subtotal.
        points = {}
        for other in candidates:
            other_id = other["option_id"]
            point = quotes[other_id].subtotal
            if point is not None and viable[other_id][0] <= point <= min(viable[other_id][1], peer_ceiling):
                points[other_id] = point
        peer = min(candidates, key=lambda other: (
            other["option_id"] not in points, points.get(other["option_id"], viable[other["option_id"]][1]),
            other["option_id"] not in assessed, other["label"].casefold(), other["label"], other["option_id"]
        ), default=None)
        peer_quote = quotes[peer["option_id"]] if peer else None
        peer_cost = points.get(peer["option_id"]) if peer else None
        peer_condition = (" The comparison depends on the other offer meeting its hard requirements."
                          if peer and any(result["result"] == "unknown" or result.get("disputed")
                                          for result in peer["constraint_results"]) else "")
        peer_note = ""
        if peer:
            peer_label = _excerpt(peer["label"], 55)
            if not peer_quote.gaps:
                peer_note = f" {peer_label} has a checked {row['horizon_months']}-month cost of {currency} {peer['total']}."
            elif peer_quote.subtotal is not None:
                bound = ("at most" if peer_quote.upper == peer_quote.subtotal else
                         "at least" if peer_quote.lower == peer_quote.subtotal else None)
                if bound:
                    peer_note = f" With its open entries, {peer_label} costs {bound} {currency} {peer_quote.subtotal:f}."
                else:
                    peer_note = f" {peer_label}'s current subtotal is {currency} {peer_quote.subtotal:f}, with open entries."
        results = {result["requirement_id"]: result for result in row["constraint_results"]}
        checked = {key for key, verdict in results.items()
                   if verdict["result"] != "unknown" and verdict["basis_refs"] and not verdict.get("disputed")}
        pending = [req for req in brief.hard_requirements if req.metric != "total_cost" and req.id not in checked
                   and not (req.metric == "lead_time_days" and identity in conflicting_delivery)]
        failure = next((req for req in sorted(brief.hard_requirements, key=lambda req: (req.description, req.id))
                        if req.id in fixed_failures[identity]), None)

        def add(priority, specificity, question, effect, method, detail, refs, gap=None):
            basis = list(dict.fromkeys(refs))
            if failure:
                effect = f"{label} already fails the hard requirement “{_excerpt(failure.description, 140)}”."
                basis = list(dict.fromkeys(basis + results[failure.id]["basis_refs"]))
            key = (priority if feasible else 5, identity != chosen, specificity, len(gaps) + len(pending),
                   option.label.casefold(), option.label, question.casefold(), question, detail, identity)
            item = {"question": question, "affected_conclusion": _excerpt(effect, 500), "verification_method": method,
                    "request_text": _excerpt(f"{question} {detail}", 4000), "basis_refs": basis[:64]}
            ranked.append((key, item, gap, peer["option_id"] if peer else None))

        # A cost limit is assessed from the same missing charges. Ask for the
        # underlying amounts once, rather than generating a second budget request.
        crossings = [req for req in cost_requirements if _can_cross(req, lower, upper)]
        current_note = (f"{label} has a {row['horizon_months']}-month current subtotal of {currency} {subtotal:f}."
                        if current_amount_refs and subtotal is not None else f"{label} has no current priced subtotal to rely on.")
        has_expired_terms = any(gap.expiry for gap in gaps)
        for gap in gaps:
            cost_moves = gap.direction != 0
            alone_min, alone_max = _range(subtotal, [gap])
            match_interval = max(cost_min, alone_min), min(cost_max, alone_max)
            direct_order = (contender and subtotal is not None and cost_moves and alone_min < alone_max and
                            _spans_price(*match_interval, peer_cost))
            siblings = [other for other in gaps if gap.line_label and other.line_label.casefold() == gap.line_label.casefold()
                        and other.direction == gap.direction and cost_moves]
            combined_order = False
            if len(siblings) > 1 and contender and subtotal is not None:
                combined_min, combined_max = _range(subtotal, siblings)
                match_interval = max(cost_min, combined_min), min(cost_max, combined_max)
                combined_order = combined_min < combined_max and _spans_price(*match_interval, peer_cost)
            direct_limits = [req for req in crossings if subtotal is not None and cost_moves
                             and _can_cross(req, alone_min, alone_max)]
            if direct_order:
                priority = 0
            elif contender and direct_limits:
                priority = 1
            elif contender and cost_moves and (order_can_change or crossings):
                priority = 2
            elif feasible and cost_moves and crossings:
                priority = 3
            else:
                priority = 4
            effect = current_note + (f" The quote expired on {gap.expiry}." if gap.expiry else "")
            refs = [row["calculation_id"], *gap.source_refs]
            if not cost_moves:
                effect += " The quoted quantity is zero, so the contribution is zero at any unit price."
            elif not feasible and lower > cost_max:
                effect += f" Even its lowest possible total of {currency} {lower:f} exceeds the limit of {currency} {cost_max:f}."
            if has_expired_terms:
                # The saved lifecycle subtotal can include expired amounts. Link
                # the remaining priced lines behind this current subtotal too.
                refs.extend(current_amount_refs)
            if peer is not None:
                if current_amount_refs and subtotal is not None and (direct_order or combined_order):
                    count = len(siblings) if combined_order else 0
                    effect += " " + _match_note(gap, peer, peer_quote, subtotal, quote.exact,
                                                len(gaps) > max(count, 1), count, label, match_interval)
                    if combined_order:
                        refs.extend(ref for sibling in siblings for ref in sibling.source_refs)
                else:
                    effect += peer_note
                effect += peer_condition
                refs.append(peer["calculation_id"])
                if peer_quote.gaps:
                    refs.extend(ref for other_gap in peer_quote.gaps for ref in other_gap.source_refs)
            target = min(direct_limits or crossings or cost_requirements,
                         key=lambda req: (abs(Decimal(req.threshold) - subtotal) if subtotal is not None else _ZERO,
                                          req.operator, req.description, req.id), default=None)
            if target:
                if target in direct_limits and subtotal > Decimal(target.threshold) and target.operator == "lte":
                    ceiling = _cost_limits([target], currency)[1]
                    effect += (f" A reduction of at least {currency} {subtotal - ceiling:f} from the current subtotal "
                               f"meets the {currency} {target.threshold} ceiling.")
                else:
                    effect += f" The required total is {_RELATION[target.operator]} {currency} {target.threshold}."
            add(priority, gap.specificity, gap.question, effect, "Obtain a current itemized quote with a validity date.",
                gap.detail, refs, gap)

        # One dated measurement can establish multiple thresholds on the same
        # delivery window. Keep every threshold and its cited evidence together.
        groups = {}
        for req in sorted(pending, key=lambda req: (req.description, req.metric, req.id)):
            groups.setdefault((req.metric, req.description), []).append(req)
        for (metric, description), requirements in groups.items():
            verdicts = [results.get(req.id, {}) for req in requirements]
            refs = [ref for verdict in verdicts for field in ("basis_refs", "counter_basis_refs")
                    for ref in verdict.get(field, [])]
            question = f"Does {option.label} meet the hard requirement “{description}”?"
            effect = f"{label} can qualify only if “{_excerpt(description, 125)}” passes or is confirmed inapplicable."
            method = "Obtain a dated statement or exact passage that establishes the requirement for this option."
            detail = "Identify the applicable offer, scope and exceptions."
            if metric == "lead_time_days":
                requirements.sort(key=lambda req: (("gte", "lte", "eq").index(req.operator), Decimal(req.threshold), req.id))
                limits = " and ".join(dict.fromkeys(f"{_RELATION[req.operator]} {req.threshold} days" for req in requirements))
                question = f"What lead time in days can {option.label} commit to for “{description}”, given the requirement of {limits}?"
                effect = f"{label}'s committed lead time must be {limits} for “{_excerpt(description, 125)}”."
                method = "Obtain a dated supplier schedule giving the committed lead time in days."
                detail = "Confirm the start and delivery milestones used to measure the lead time."
            if row["total"] is not None and not gaps:
                effect += f" Its checked {row['horizon_months']}-month cost is {currency} {row['total']}."
                refs.append(row["calculation_id"])
            if peer_note:
                effect += peer_note + peer_condition
                refs.append(peer["calculation_id"])
            if any(verdict.get("disputed") for verdict in verdicts):
                detail += " Reconcile the conflicting assessments against their cited passages."
            add(1 if contender else 3, 0, question, effect, method, detail, refs)

    ordered = sorted(ranked, key=lambda pair: pair[0])
    items = owner_requests + [item for _, item, _, _ in ordered] + deferred_owner_requests
    requests = [{"id": f"next_evidence_{index}", **item} for index, item in enumerate(items, start=1)]
    # Priorities zero to two can change a contender's price order or eligibility.
    # Other requests remain available for completing the underlying records.
    quoted = {identity: (quote.lower, quote.upper) for identity, quote in quotes.items()}
    lead_question = lead_option_id = lead_cost_gap = lead_peer_id = None
    if owner_requests:
        lead_question = requests[0]
    elif ordered:
        first_key, _, first_gap, first_peer = ordered[0]
        if first_key[0] <= 2:
            lead_question, lead_option_id = requests[0], first_key[-1]
            lead_cost_gap, lead_peer_id = first_gap, first_peer
    return QuestionWorklist(requests, lead_question, lead_option_id, viable, quoted, lead_cost_gap, lead_peer_id)
