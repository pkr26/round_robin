"""Bounded Decimal arithmetic; formulas are descriptive output, never executable input."""

from datetime import date, datetime, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
from typing import Literal, Self

from pydantic import Field, model_validator

from round_robin.schemas.briefs import Option
from round_robin.schemas.common import Contract, checked_decimal
from round_robin.schemas.evidence import Calculation, CalculationInput
from .currencies import minor_units

MAX_INTERMEDIATE = Decimal("1e30")


class CalculationError(ValueError):
    pass


def _label(value: str, maximum: int = 500) -> str:
    """Bound derived display labels while source text stays in the saved brief."""
    return value if len(value) <= maximum else value[:maximum - 1] + "…"


class Operand(Contract):
    amount: str
    unit: str = Field(min_length=1, max_length=16)
    kind: Literal["money", "quantity", "rate"]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        maximum, places = {"money": ("1000000000000", 6), "quantity": ("1000000000", 6), "rate": ("1000000", 12)}[self.kind]
        checked_decimal(self.amount, maximum=maximum, places=places, positive=self.kind == "rate")
        return self


class Operation(Contract):
    operation: Literal["add", "subtract", "multiply", "divide"]
    operands: list["Operand | Operation"] = Field(min_length=2, max_length=120)


def _bounded(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) > MAX_INTERMEDIATE:
        raise CalculationError("Intermediate magnitude exceeds 10^30")
    return value


def evaluate(operation: Operation) -> tuple[str, str, list[str]]:
    """Evaluate at most 120 leaves and four operation levels with dimension checks."""
    count = 0
    division_rounded = False

    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = 64
        context.rounding = ROUND_HALF_EVEN
        context.traps[InvalidOperation] = True
        context.traps[Overflow] = True

        def visit(node: Operand | Operation, depth: int) -> tuple[Decimal, str]:
            nonlocal count, division_rounded
            if isinstance(node, Operand):
                count += 1
                if count > 120:
                    raise CalculationError("At most 120 operands are supported")
                return Decimal(node.amount), node.unit
            if depth > 4:
                raise CalculationError("At most four operation levels are supported")
            values = [visit(item, depth + 1) for item in node.operands]
            value, unit = values[0]
            for right, right_unit in values[1:]:
                context.clear_flags()
                if node.operation in {"add", "subtract"}:
                    if unit != right_unit:
                        raise CalculationError("Addition/subtraction require compatible units and currencies")
                    value = value + right if node.operation == "add" else value - right
                elif node.operation == "multiply":
                    if unit != "dimensionless" and right_unit != "dimensionless":
                        raise CalculationError("Multiplication supports only a dimensionless scale")
                    value *= right
                    unit = right_unit if unit == "dimensionless" else unit
                else:
                    if right == 0:
                        raise CalculationError("Division by zero is forbidden")
                    if right_unit == unit:
                        unit = "dimensionless"
                    elif right_unit != "dimensionless":
                        raise CalculationError("Division requires matching units or a dimensionless divisor")
                    value /= right
                if context.flags[Inexact]:
                    if node.operation != "divide":
                        raise CalculationError("Inexact nondivision intermediate is unsupported")
                    division_rounded = True
                _bounded(value)
            return value, unit

        value, unit = visit(operation, 1)
        rendered = format(value, "f")
        if len(rendered) > 96:
            raise CalculationError("Serialized result exceeds 96 characters")
        return rendered, unit, ["division rounded at Decimal precision 64"] if division_rounded else []


def lifecycle_total(option: Option, horizon_months: int, currency: str, *, calculation_id: str, as_of: date | None = None) -> Calculation:
    if isinstance(horizon_months, bool) or not isinstance(horizon_months, int) or not 1 <= horizon_months <= 120:
        raise CalculationError("Horizon must be an integer from 1 to 120 months")
    as_of = as_of if as_of is not None else datetime.now(timezone.utc).date()
    if type(as_of) is not date:
        raise CalculationError("Quote freshness requires an explicit calendar date")
    try:
        places = minor_units(currency)
    except ValueError as error:
        raise CalculationError(str(error)) from None
    inputs = []
    missing = []
    terms = []
    if not any(component.status in {"included", "excluded"} for component in option.cost_components):
        missing.append("No included cost components were supplied; an explicit zero is required to establish zero cost.")
    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = 64
        context.rounding = ROUND_HALF_EVEN
        context.traps[Inexact] = True
        context.traps[InvalidOperation] = True
        context.traps[Overflow] = True
        total = Decimal(0)
        for component in option.cost_components:
            if component.status == "excluded":
                missing.append(f"{_label(component.label, 380)}: excluded from the supplied cost; confirm the applicable charge or mark it not applicable.")
                continue
            if component.status != "included":
                continue
            if component.currency != currency:
                raise CalculationError("Currency conversion requires a separately dated rate; mixed currencies cannot be totaled")
            expired = component.quote_valid_until is not None and component.quote_valid_until < as_of
            if component.amount is None:
                detail = f"{_label(component.label, 200 if expired else 240)}: {_label(component.unknown_reason or 'Unknown', 220 if expired else 258)}"
                if expired:
                    detail = f"Quote expired {component.quote_valid_until}; obtain a current quote. {detail}"
                missing.append(_label(detail, 500))
                continue
            if expired:
                missing.append(f"{_label(component.label, 380)}: quote expired {component.quote_valid_until}; obtain a current quote for this comparison.")
            months = horizon_months if component.frequency == "monthly" else 1
            value = _bounded(Decimal(component.amount) * Decimal(component.quantity) * months)
            total = _bounded(total - value if component.kind == "discount" else total + value)
            inputs.append(CalculationInput(label=component.label, amount=component.amount, unit=currency, source_ref=component.source_ref))
            sign = "−" if component.kind == "discount" else "+"
            terms.append(f"{sign} {component.amount} × {component.quantity}" + (f" × {horizon_months} months" if component.frequency == "monthly" else ""))
        if total < 0:
            raise CalculationError("Discounts cannot create a negative all-in cost")
        context.traps[Inexact] = False
        rounded = total.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
        flags = ["final currency rounding"] if context.flags[Inexact] else []
        known = format(rounded, f".{places}f")
    return Calculation(id=calculation_id, label=f"{_label(option.label, 470)}: {horizon_months}-month cost", operation="lifecycle_total", inputs=inputs, formula=" ".join(terms).lstrip("+ ") or "No included cost amounts", output=None if missing else known, unit=currency, currency=currency, horizon_months=horizon_months, rounding=f"{currency} minor unit ({places} decimal places), ROUND_HALF_EVEN (after exact component arithmetic)", flags=flags, missing_components=missing, known_subtotal=known)


def break_even(first: Option, second: Option, currency: str, *, as_of: date | None = None) -> Calculation:
    as_of = as_of if as_of is not None else datetime.now(timezone.utc).date()
    if type(as_of) is not date:
        raise CalculationError("Quote freshness requires an explicit calendar date")

    def rates(option: Option) -> tuple[Decimal, Decimal]:
        if not any(component.status == "included" for component in option.cost_components) or any(component.status == "excluded" for component in option.cost_components):
            raise CalculationError("Break-even requires explicit complete costs in both options")
        upfront, monthly = Decimal(0), Decimal(0)
        for component in option.cost_components:
            if component.status != "included":
                continue
            if component.amount is None or component.currency != currency:
                raise CalculationError("Break-even requires complete costs in one currency")
            if component.quote_valid_until is not None and component.quote_valid_until < as_of:
                raise CalculationError("Break-even requires current quotes as of the comparison date")
            amount = Decimal(component.amount) * Decimal(component.quantity)
            amount = -amount if component.kind == "discount" else amount
            if component.frequency == "monthly":
                monthly += amount
            else:
                upfront += amount
        return upfront, monthly

    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)) as context:
        a_upfront, a_monthly = rates(first)
        b_upfront, b_monthly = rates(second)
        divisor = a_monthly - b_monthly
        if divisor == 0:
            raise CalculationError("No unique break-even month exists for equal recurring costs")
        result = _bounded((b_upfront - a_upfront) / divisor)
        if result < 0:
            raise CalculationError("No nonnegative break-even month exists")
        rendered = format(result, "f")
        if len(rendered) > 96:
            raise CalculationError("Break-even result exceeds output bounds")
        flags = ["division rounded at Decimal precision 64"] if context.flags[Inexact] else []
    inputs = [CalculationInput(label=f"{_label(option.label, 240)}: {_label(component.label, 258)}", amount=component.amount, unit=f"{currency}/month" if component.frequency == "monthly" else currency, source_ref=component.source_ref) for option in (first, second) for component in option.cost_components if component.status == "included"]
    return Calculation(id="calc_break_even", label="A/B break-even horizon", operation="break_even", inputs=inputs, formula=f"({b_upfront} − {a_upfront}) ÷ ({a_monthly} − {b_monthly})", output=rendered, unit="months", rounding="Decimal precision 64; division flags retained", flags=flags)
