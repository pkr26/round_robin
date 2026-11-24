"""Conserved, serializable visual counters and unresolved exposure.

Persist this state under a locked design session before external I/O. The live
gateway additionally must reserve against the shared owner/deployment ledger;
this local reducer cannot authorize a paid provider by itself.
"""
from dataclasses import dataclass, field
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math

from round_robin.orchestration.budget import _exact_money


class VisualLimit(ValueError):
    pass


def decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        return result
    except (InvalidOperation, TypeError, ValueError) as error:
        raise VisualLimit("A finite nonnegative monetary bound is required") from error


@dataclass
class VisualState:
    mode: str
    maximum_usd: str
    admitted_at: float
    request_count: int = 0
    submission_count: int = 0
    poll_count: int = 0
    version_count: int = 0
    intentional_edits: int = 0
    renderer_attempts: int = 0
    paid_admitted: bool = True
    fence: int = 0
    epoch: int = 1
    status: str = "active"
    step_calls: dict[str, int] = field(default_factory=dict)
    operations: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        # Reducers must never mutate the ORM's loaded JSON value in place.
        # Otherwise reassigning an equal snapshot can lose nested-only usage
        # settlements because SQLAlchemy observes no change to persist.
        self.operations = deepcopy(self.operations)
        self.step_calls = dict(self.step_calls)
        decimal(self.maximum_usd)
        counters = (self.request_count, self.submission_count, self.poll_count, self.version_count, self.intentional_edits, self.renderer_attempts, self.fence, self.epoch)
        if any(type(value) is not int for value in counters) or type(self.paid_admitted) is not bool:
            raise VisualLimit("Persisted visual counters and execution authority require exact integers/booleans")
        if self.mode not in {"review_only", "improve"} or self.status not in {"active", "cancelled", "completed", "quarantined", "deleted", "expired"}:
            raise VisualLimit("Unsupported visual mode or persisted status")
        if not math.isfinite(self.admitted_at) or self.admitted_at < 0 or self.epoch < 1 or self.fence < 0:
            raise VisualLimit("Invalid persisted admission clock or execution authority")
        if not (0 <= self.request_count <= 24 and 0 <= self.submission_count <= 4 and 0 <= self.poll_count <= 60 and 0 <= self.version_count <= 3 and 0 <= self.intentional_edits <= 2 and 0 <= self.renderer_attempts <= 6):
            raise VisualLimit("Invalid persisted visual counters")
        calls = {}
        for operation in self.operations.values():
            if operation.get("kind") not in {"generation", "edit", "vision"} or operation.get("status") not in {"authorized", "unknown", "settled"}:
                raise VisualLimit("Unsupported persisted visual operation kind or outcome")
            if operation["status"] == "settled" and operation.get("actual") is None:
                raise VisualLimit("A settled visual operation requires its actual usage")
            decimal(operation["bound"])
            if operation.get("actual") is not None:
                decimal(operation["actual"])
            if operation["kind"] in {"generation", "edit", "vision"}:
                step = operation["step_id"]
                calls[step] = calls.get(step, 0) + 1
        if self.request_count != sum(calls.values()) or self.submission_count != sum(op["kind"] in {"generation", "edit"} for op in self.operations.values()) or self.step_calls != calls or any(count > 2 for count in calls.values()):
            raise VisualLimit("Persisted authorization journal and visual counters disagree")

    @_exact_money
    def exposure(self) -> Decimal:
        return sum((decimal(operation["actual"]) if operation["status"] == "settled" else max(decimal(operation["bound"]), decimal(operation.get("actual") or "0")) for operation in self.operations.values()), Decimal("0"))

    @_exact_money
    def authorize(self, operation_id: str, step_id: str, kind: str, bound_usd: str | None, *, now: float, expected_fence: int, epoch: int, review_money: str = "0", review_calls: int = 2, review_seconds: float = 190, request_seconds: float = 0) -> None:
        if kind not in {"generation", "edit", "vision"}:
            raise VisualLimit("Unsupported visual operation kind")
        if not self.paid_admitted or self.status != "active" or self.fence != expected_fence or self.epoch != epoch:
            raise VisualLimit("Visual session is terminal or fenced")
        if not math.isfinite(now) or now < self.admitted_at or now - self.admitted_at >= 1800 or 1800 - (now - self.admitted_at) <= review_seconds + 25:
            raise VisualLimit("Original visual time or protected review time is exhausted")
        if not math.isfinite(request_seconds) or request_seconds < 0 or request_seconds + review_seconds + 25 > 1800 - (now - self.admitted_at):
            raise VisualLimit("Request timeout would spend the protected review or persistence time")
        if bound_usd is None:
            raise VisualLimit("Unknown image/vision pricing cannot authorize dispatch")
        bound = decimal(bound_usd)
        if operation_id in self.operations:
            raise VisualLimit("Attempt identity was already authorized; resolve its original outcome")
        if kind in {"generation", "edit"}:
            if self.mode != "improve" or self.submission_count >= 4:
                raise VisualLimit("Generation is outside the frozen mode or submission ceiling")
            if kind == "edit" and (self.version_count == 0 or self.intentional_edits >= 2):
                raise VisualLimit("An edit requires an existing version and remaining intentional edit allowance")
        model_call = kind in {"generation", "edit", "vision"}
        if model_call and (self.step_calls.get(step_id, 0) >= 2 or self.request_count + 1 + review_calls > 24):
            raise VisualLimit("Shared inference/step limit or protected review calls are exhausted")
        if self.exposure() + bound + decimal(review_money) > decimal(self.maximum_usd):
            raise VisualLimit("Money or protected next-review headroom is exhausted")
        self.operations[operation_id] = {"kind": kind, "step_id": step_id, "bound": str(bound), "actual": None, "status": "authorized", "authorized_at": now}
        if model_call:
            self.request_count += 1
            self.step_calls[step_id] = self.step_calls.get(step_id, 0) + 1
        if kind in {"generation", "edit"}:
            self.submission_count += 1

    def settle(self, operation_id: str, actual_usd: str | None) -> None:
        operation = self.operations[operation_id]
        if actual_usd is None:
            if operation["status"] != "settled":
                operation["status"] = "unknown"
            return
        actual = decimal(actual_usd)
        if operation["status"] == "settled":
            if decimal(operation["actual"]) != actual:
                raise VisualLimit("Conflicting settlement requires explicit evidenced reconciliation")
            return
        operation.update(actual=str(actual), status="settled")

    def publish_version(self, *, parent_hash: str | None, expected_parent_hash: str | None) -> int:
        if self.status != "active" or self.version_count >= 3 or parent_hash != expected_parent_hash:
            raise VisualLimit("Stale/terminal version or V1–V3 ceiling")
        if self.version_count > 0:
            if self.mode != "improve" or parent_hash is None or self.intentional_edits >= 2:
                raise VisualLimit("A new edited version requires exact parent lineage")
            self.intentional_edits += 1
        elif parent_hash is not None:
            raise VisualLimit("The original version has no parent")
        self.version_count += 1
        return self.version_count

    def poll(self, *, now: float) -> None:
        if self.status != "active" or not math.isfinite(now) or now < self.admitted_at or now - self.admitted_at >= 1800 or self.poll_count >= 60:
            raise VisualLimit("Original visual deadline or sixty-poll allowance is exhausted")
        self.poll_count += 1

    def cancel(self) -> None:
        self.status = "cancelled"
        self.fence += 1
