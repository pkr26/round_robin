"""Conservative operation ledger shared by every paid entry point.

Transactions lock deployment policy, owner policy, run, then step. Callers commit
authorization before external I/O. Unknown exposure is carried, including across
UTC period rollover; a conservative accounting booking cannot settle uncertainty.
"""

from datetime import datetime, timezone
from decimal import Context, Decimal, DecimalException, InvalidOperation, localcontext
from functools import wraps

from sqlalchemy import select
from sqlalchemy.orm import Session

from round_robin.database.models import BudgetOperation, Meeting, Run, Step, utcnow
from round_robin.database.services import lock_policies, locked_run


class BudgetDenied(RuntimeError):
    pass


def _exact_money(function):
    """Isolate bounded ledger arithmetic from caller precision and traps."""
    @wraps(function)
    def calculate(*args, **kwargs):
        # Runtime amounts fit 30 integer and 24 fractional digits. Leave ample
        # room for aggregate sums, rates and conservative reservation transfers.
        with localcontext(Context(prec=128)):
            return function(*args, **kwargs)
    return calculate


def amount(value) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (ValueError, InvalidOperation) as exc:
        raise BudgetDenied("invalid_money") from exc


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def active_elapsed(run: Run, now=None) -> float:
    now = aware(now or utcnow())
    elapsed = float(run.active_seconds or 0)
    if run.active_started_at:
        delta = (now - aware(run.active_started_at)).total_seconds()
        if delta < -1:
            raise BudgetDenied("clock_regression")
        elapsed += max(0, delta)
    return elapsed


def remaining_active(run: Run, now=None) -> float:
    if run.configuration.get("active_clock_invalid"):
        raise BudgetDenied("clock_regression")
    return float(run.configuration.get("limits", {}).get("active_seconds", 600)) - active_elapsed(run, now)


def freeze_active(run: Run, now=None) -> None:
    run.active_seconds = active_elapsed(run, now)
    run.active_started_at = None


def model_call_count(operations: list[BudgetOperation]) -> int:
    """Count model calls only when every record's metadata is readable."""
    if any(not isinstance(operation.metadata_json, dict)
           or (operation.kind == "model" and "model_call" not in operation.metadata_json)
           or ("model_call" in operation.metadata_json and type(operation.metadata_json["model_call"]) is not bool)
           for operation in operations):
        raise BudgetDenied("ledger_integrity_failed")
    return sum(operation.metadata_json.get("model_call") is True for operation in operations)


def ledger_amount(value) -> Decimal:
    """Read a stored liability only within the runtime accounting bounds."""
    try:
        if len(str(value)) > 64:
            raise ValueError
        result = amount(value)
        if result > Decimal("1e30") or not -24 <= result.as_tuple().exponent <= 30:
            raise ValueError
        return result
    except (BudgetDenied, DecimalException, ValueError, TypeError):
        raise BudgetDenied("ledger_integrity_failed") from None


@_exact_money
def operations_exposure(operations: list[BudgetOperation], *, now=None, period=None) -> Decimal:
    now = aware(now or utcnow())
    total = Decimal("0")
    for operation in operations:
        if operation.status == "released":
            continue
        if operation.status == "settled":
            authorized = aware(operation.authorized_at)
            if period == "daily" and authorized.date() != now.date():
                continue
            if period == "monthly" and (authorized.year, authorized.month) != (now.year, now.month):
                continue
            total += ledger_amount(operation.settled_usd)
        else:
            # Booking is metadata only. The uncertain operation is counted once.
            if not isinstance(operation.metadata_json, dict):
                raise BudgetDenied("ledger_integrity_failed")
            total += max(ledger_amount(operation.reserved_usd), ledger_amount(operation.settled_usd or "0"), ledger_amount(operation.metadata_json.get("conservative_booking_usd", "0")))
    return total


@_exact_money
def validate_run_ledger(operations: list[BudgetOperation], *, simulated: bool) -> int:
    """Validate prospective report usage without changing any ledger record."""
    from pydantic import ValidationError
    from round_robin.schemas.runtime import RuntimeUsage
    try:
        calls = model_call_count(operations)
        def bounded_operand(value):
            number = ledger_amount(value)
            if number > Decimal("1e30") or not -24 <= number.as_tuple().exponent <= 30:
                raise ValueError("unavailable usage")
        for operation in operations:
            if operation.status != "released":
                if operation.status == "settled":
                    bounded_operand(operation.settled_usd)
                else:
                    bounded_operand(operation.reserved_usd)
                    bounded_operand(operation.settled_usd or "0")
                    bounded_operand(operation.metadata_json.get("conservative_booking_usd", "0"))
            # Released amounts still contribute to the existing actual-cost
            # calculation when no operation remains unknown.
            if operation.kind != "finish_hold":
                bounded_operand(operation.settled_usd or "0")
        exposure = operations_exposure(operations)
        unknown = [operation.id for operation in operations if operation.kind != "finish_hold" and operation.status in {"reserved", "unknown"}]
        actual = None if unknown else sum((Decimal(operation.settled_usd or "0") for operation in operations if operation.kind != "finish_hold"), Decimal("0"))
        # Never format an attacker-controlled extreme exponent into an
        # unbounded string before enforcing the runtime contract's limits.
        def bounded(value):
            if not value.is_finite() or value < 0 or value > Decimal("1e30") or not -24 <= value.as_tuple().exponent <= 30:
                raise ValueError("unavailable usage")
            return format(value, "f")
        RuntimeUsage.model_validate({"simulated": simulated, "status": "unknown" if unknown else "reported", "actual_cost": None if actual is None else bounded(actual),
            "conservative_exposure_usd": bounded(exposure), "unknown_operation_ids": unknown, "request_count": calls, "currency": "USD"})
        return calls
    except (BudgetDenied, DecimalException, ValidationError, ValueError, TypeError, AttributeError):
        raise BudgetDenied("ledger_integrity_failed") from None


@_exact_money
def _policy_checks(session: Session, run: Run, extra: Decimal, *, now=None):
    from .protocol import verify_policy
    if not verify_policy(run.configuration):
        raise BudgetDenied("discussion_policy_incompatible")
    from round_robin.operations.durability import marker_pending
    if marker_pending(session=session):
        raise BudgetDenied("durability_compromised")
    from .configuration import verify_frozen_configuration
    verify_frozen_configuration(run)
    deployment, owner = lock_policies(session, run.owner_id)
    if deployment.metadata_json.get("dispatch_locked") or deployment.metadata_json.get("restore_quarantine"):
        raise BudgetDenied("dispatch_paused")
    if deployment.metadata_json.get("deployment_epoch", run.deployment_epoch) != run.deployment_epoch:
        raise BudgetDenied("execution_epoch_changed")
    if run.configuration.get("mode", "simulated") == "simulated":
        if extra != 0:
            raise BudgetDenied("simulated_cost_must_be_zero")
        return deployment, owner
    if run.configuration.get("funded") is not True or deployment.spending_locked or owner.spending_locked:
        raise BudgetDenied("spending_locked")
    frozen_epoch = run.deployment_epoch
    if deployment.metadata_json.get("deployment_epoch", frozen_epoch) != frozen_epoch:
        raise BudgetDenied("execution_epoch_changed")
    all_operations = list(session.scalars(select(BudgetOperation).execution_options(populate_existing=True)))
    owner_operations = [operation for operation in all_operations if operation.owner_id == run.owner_id]
    run_operations = [operation for operation in owner_operations if operation.run_id == run.id]
    limits = run.configuration.get("limits", {})
    if operations_exposure(run_operations, now=now) + extra > amount(limits.get("per_run_usd", "2")):
        raise BudgetDenied("run_budget_exhausted")
    for policy, operations, prefix in ((deployment, all_operations, "deployment"), (owner, owner_operations, "owner")):
        for period, default in (("daily", "10"), ("monthly", "50")):
            configured = policy.metadata_json.get(f"{period}_usd", policy.limit_usd if period == "daily" else "0")
            key = f"{prefix}_{period}_usd"
            frozen = limits.get(key, run.configuration.get("aggregate_funding", {}).get(key, default))
            ceiling = min(amount(configured), amount(frozen))
            if operations_exposure(operations, now=now, period=period) + extra > ceiling:
                raise BudgetDenied(f"{prefix}_{period}_budget_exhausted")
    return deployment, owner


@_exact_money
def checked_finish_hold(session: Session, run: Run, *, lock: bool = False, for_release: bool = False) -> BudgetOperation | None:
    """Reject a misbound or ambiguous canonical finish hold without altering it."""
    query = select(BudgetOperation).where(BudgetOperation.id == f"finish_{run.id}")
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    hold = session.scalar(query)
    if hold is None:
        return None
    active = not for_release and run.status in {"queued", "running", "awaiting_input"}
    if (hold.run_id != run.id or hold.owner_id != run.owner_id or hold.kind != "finish_hold"
            or hold.status not in ({"held"} if active else {"held", "released"})
            or not isinstance(hold.metadata_json, dict)):
        raise BudgetDenied("ledger_integrity_failed")
    metadata = hold.metadata_json
    if set(metadata) - {"roles_remaining", "initial_reserved_usd", "correction_reserved_usd", "conservative_booking_usd"}:
        raise BudgetDenied("ledger_integrity_failed")
    try:
        reserved = ledger_amount(hold.reserved_usd)
        settled = ledger_amount(hold.settled_usd) if hold.settled_usd is not None else Decimal("0")
        booked = ledger_amount(metadata.get("conservative_booking_usd", "0"))
        roles = metadata.get("roles_remaining", [])
        if (settled != 0 or booked != 0 or (hold.status == "released" and reserved != 0)
                or not isinstance(roles, list) or any(role not in {"synthesis", "audit"} for role in roles)
                or len(set(roles)) != len(roles)):
            raise BudgetDenied("ledger_integrity_failed")
        initial = ledger_amount(metadata["initial_reserved_usd"]) if "initial_reserved_usd" in metadata else None
        if ("roles_remaining" in metadata) != ("initial_reserved_usd" in metadata):
            raise BudgetDenied("ledger_integrity_failed")
        if "correction_reserved_usd" in metadata and (initial is None or ledger_amount(metadata["correction_reserved_usd"]) != initial):
            raise BudgetDenied("ledger_integrity_failed")
        if active:
            if "roles_remaining" not in metadata or initial is None or not isinstance(run.configuration, dict):
                raise BudgetDenied("ledger_integrity_failed")
            expected = ledger_amount("0" if run.configuration.get("mode", "simulated") == "simulated" else run.configuration.get("finish_policy", {}).get("reserved_usd", "0.40"))
            if initial != expected:
                raise BudgetDenied("ledger_integrity_failed")
            if reserved < initial * len(roles) / 2:
                raise BudgetDenied("finish_money_reserve")
    except BudgetDenied as error:
        if str(error) == "finish_money_reserve":
            raise
        raise BudgetDenied("ledger_integrity_failed") from None
    except (ValueError, TypeError, AttributeError):
        raise BudgetDenied("ledger_integrity_failed") from None
    return hold


def reserve_finish(session: Session, run: Run) -> BudgetOperation:
    operation_id = f"finish_{run.id}"
    existing = checked_finish_hold(session, run, lock=True)
    if existing:
        return existing
    config = run.configuration
    reserved = amount("0" if config.get("mode", "simulated") == "simulated" else config.get("finish_policy", {}).get("reserved_usd", "0.40"))
    _policy_checks(session, run, reserved)
    operation = BudgetOperation(id=operation_id, run_id=run.id, owner_id=run.owner_id, kind="finish_hold", status="held", reserved_usd=str(reserved), settled_usd=None, authorized_at=utcnow(), metadata_json={"roles_remaining": ["synthesis", "audit"], "initial_reserved_usd": str(reserved)})
    session.add(operation)
    session.flush()
    return operation


@_exact_money
def authorize_operation(session: Session, run: Run, operation_id: str, kind: str, bound_usd, *, step: Step | None = None, finish_role: str | None = None, model_call: bool = True) -> BudgetOperation:
    # Lock hierarchy is also used by cancellation and policy changes.
    lock_policies(session, run.owner_id)
    current = locked_run(session, run.id)
    if current is None or current.status != "running":
        raise BudgetDenied("run_ineligible")
    meeting = session.get(Meeting, run.meeting_id)
    if not meeting or meeting.deleted_at:
        raise BudgetDenied("run_fenced")
    if bound_usd is None:
        raise BudgetDenied("unknown_price")
    bound = amount(bound_usd)
    if session.get(BudgetOperation, operation_id):
        raise BudgetDenied("operation_already_authorized")
    limits = current.configuration.get("limits", {})
    if step is not None:
        step = session.scalar(select(Step).where(Step.id == step.id).with_for_update().execution_options(populate_existing=True))
        if step is None or step.run_id != current.id:
            raise BudgetDenied("foreign_step")
    operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == current.id).execution_options(populate_existing=True)))
    model_calls = model_call_count(operations)
    hold = reserve_finish(session, current)
    roles = list(hold.metadata_json.get("roles_remaining", []))
    transfer = finish_role in roles
    if finish_role not in {None, "synthesis", "audit"}:
        raise BudgetDenied("invalid_finish_role")
    if model_call:
        role_steps = list(session.scalars(select(Step).where(Step.run_id == current.id, Step.stage == step.stage, Step.round == step.round, Step.role_slot_id == step.role_slot_id).execution_options(populate_existing=True))) if step else []
        role_calls = sum(item.calls for item in role_steps)
        if step is None or role_calls >= min(2, int(limits.get("max_calls_per_step", 2))):
            raise BudgetDenied("step_call_limit")
        protected = len(roles) - int(transfer)
        if model_calls + 1 + protected > min(24, int(limits.get("max_requests", 24))):
            raise BudgetDenied("finish_call_reserve")
    from .protocol import mandatory_tail_seconds
    configured_seconds = current.configuration.get("finish_policy", {}).get("reserved_active_seconds", 190)
    finish_seconds = (mandatory_tail_seconds("proposal", configured_seconds)
                      if current.configuration.get("discussion_policy") else float(configured_seconds))
    protected_seconds = finish_seconds * (len(roles) - int(transfer)) / 2
    if remaining_active(current) <= protected_seconds + 25:
        raise BudgetDenied("finish_time_reserve")
    extra = bound
    if transfer:
        # Each first final role receives at most its predeclared allocation.
        allocation = amount(hold.metadata_json["initial_reserved_usd"]) / 2
        if bound > allocation or amount(hold.reserved_usd) < bound:
            raise BudgetDenied("finish_money_reserve")
        extra = Decimal("0")
    _policy_checks(session, current, extra)
    if transfer:
        hold.reserved_usd = str(amount(hold.reserved_usd) - bound)
        hold.metadata_json = {**hold.metadata_json, "roles_remaining": [role for role in roles if role != finish_role]}
    if step is not None and model_call:
        step.calls += 1
    operation = BudgetOperation(id=operation_id, run_id=current.id, owner_id=current.owner_id, kind=kind, status="reserved", reserved_usd=str(bound), settled_usd=None, authorized_at=utcnow(), metadata_json={"model_call": model_call, "step_id": step.id if step else None, "finish_role": finish_role, "epoch": current.deployment_epoch})
    session.add(operation)
    session.flush()
    return operation


def reconcile_operation(session: Session, operation_id: str, reported_usd, certainty: str = "reported") -> BudgetOperation:
    operation = session.get(BudgetOperation, operation_id)
    if not operation:
        raise BudgetDenied("unknown_operation")
    # Callers hold policy/run locks; late usage reconciliation is allowed after a
    # fence, but cannot publish content or reopen work.
    if certainty not in {"reported", "unknown", "proven_not_sent"}:
        raise BudgetDenied("invalid_usage_certainty")
    if certainty == "proven_not_sent":
        reported_usd = "0"
    if certainty == "reported" and reported_usd is None:
        certainty = "unknown"
    value = amount(reported_usd) if reported_usd is not None else None
    if operation.status == "settled":
        if value is not None and value != amount(operation.settled_usd):
            raise BudgetDenied("usage_reconciliation_conflict")
        return operation
    if value is not None:
        # A nonfinal observation cannot refund an earlier higher observed
        # charge. Only a trusted final receipt or proof of no send may reduce
        # exposure; otherwise retain the high-water mark across reconciliation.
        operation.settled_usd = str(max(value, amount(operation.settled_usd or "0")) if certainty == "unknown" else value)
    operation.status = "settled" if certainty in {"reported", "proven_not_sent"} else "unknown"
    return operation


def release_unused_finish(session: Session, run: Run) -> bool:
    try:
        hold = checked_finish_hold(session, run, lock=True, for_release=True)
    except BudgetDenied:
        return False
    if hold and hold.status == "held":
        hold.reserved_usd = "0"
        hold.status = "released"
        return True
    return False
