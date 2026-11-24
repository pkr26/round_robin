import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from round_robin.database.models import BudgetOperation, Step, utcnow

from .budget import BudgetDenied, aware, checked_finish_hold, validate_run_ledger, remaining_active

MANUAL_RETRY_CODES = {"connection_failure", "timeout", "rate_limited", "rate_limit", "provider_unavailable", "server_error", "transient", "transport_failure", "response_limit", "schema_invalid", "invalid_reference", "missing_audit_result", "malformed_response", "truncated", "empty", "worker_lost", "worker_lost_after_authorization", "lease_expired", "persistence_failed"}


def retryable_steps(session, run, *, steps=None):
    """One eligibility policy for visible Retry and retained finish money."""
    if run.status not in {"failed", "completed_with_limitations"}:
        return []
    if (run.configuration.get("invalid_frozen_inputs") is True
            or run.configuration.get("discussion_integrity_failed") is True
            or run.configuration.get("ledger_integrity_failed") is True
            or run.configuration.get("saved_state_unavailable") is True):
        return []
    from .configuration import verify_frozen_briefs, verify_saved_discussion
    try:
        verify_frozen_briefs(run)
        verify_saved_discussion(session, run)
    except BudgetDenied:
        return []
    if run.configuration.get("retry_abandoned"):
        return []
    if run.configuration.get("mode", "simulated") == "live":
        # Pausing freezes active execution time, not the immutable funding
        # authorization's wall-clock expiry. No later operator file can renew
        # the deadline of this already accepted run.
        try:
            if aware(datetime.fromisoformat(run.configuration["expires_at"])) <= utcnow():
                return []
        except (KeyError, TypeError, ValueError):
            return []
    try:
        remaining = remaining_active(run)
    except BudgetDenied:
        return []
    if remaining <= 25:
        return []
    steps = steps if steps is not None else list(session.scalars(select(Step).where(Step.run_id == run.id)))
    limits = run.configuration.get("limits", {})
    try:
        checked_finish_hold(session, run)
        ledger_calls = validate_run_ledger(list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id))), simulated=run.configuration.get("mode", "simulated") == "simulated")
    except BudgetDenied:
        return []
    calls = max(sum(step.calls for step in steps), ledger_calls)
    maximum_requests = min(24, limits.get("max_requests", 24))
    if calls >= maximum_requests:
        return []
    call_limit = min(2, limits.get("max_calls_per_step", 2))
    iteration = run.configuration.get("stage_iteration", 0)
    hold = session.get(BudgetOperation, f"finish_{run.id}")
    roles = hold.metadata_json.get("roles_remaining", []) if hold else ["synthesis", "audit"]
    finish_seconds = run.configuration.get("finish_policy", {}).get("reserved_active_seconds", 190)
    result = []
    for step in steps:
        if step.stage not in {"synthesis", "audit"} or step.stage_iteration != iteration or step.status != "failed" or step.error_code not in MANUAL_RETRY_CODES:
            continue
        if step.stage == "synthesis" and any(other.stage == "audit" and other.status == "succeeded" for other in steps):
            # Legacy/inconsistent history cannot reuse an audit that never saw
            # a replacement synthesis. A linked run provides a new full review.
            continue
        logical = [other for other in steps if (other.stage, other.round, other.role_slot_id) == (step.stage, step.round, step.role_slot_id)]
        protected = len(roles) - int(step.stage in roles)
        request_time = finish_seconds / 2 + 25 if step.stage == "synthesis" else 25
        if (sum(other.infrastructure_claims for other in logical) < 3 and sum(other.calls for other in logical) < call_limit
                and calls + 1 + protected <= maximum_requests
                and remaining > max(request_time, finish_seconds * protected / 2 + 25)):
            result.append(step)
    return result


@dataclass(frozen=True)
class Recovery:
    retry: bool
    delay_seconds: float = 0
    fallback_provider: str | None = None
    clock_regressed: bool = False


def plan_recovery(run, step, failure, *, synthesizer=None, logical_calls=None, logical_claims=None, capacity_delay_seconds=0):
    calls = step.calls if logical_calls is None else logical_calls
    claims = step.infrastructure_claims if logical_claims is None else logical_claims
    call_limit = min(2, int(run.configuration.get("limits", {}).get("max_calls_per_step", 2)))
    if claims >= 3 or calls >= call_limit:
        return Recovery(False)

    def fits(wait):
        from .protocol import recovery_fits
        try:
            remaining = remaining_active(run)
        except BudgetDenied as error:
            if str(error) != "clock_regression":
                raise
            return None
        return recovery_fits(stage=step.stage, remaining_seconds=remaining, wait_seconds=wait,
                             reserved_finish_seconds=run.configuration.get("finish_policy", {}).get("reserved_active_seconds", 190),
                             capacity_delay_seconds=capacity_delay_seconds)

    if not failure.retryable:
        # Moderator transport/config unavailability can use only the existing
        # second call. Refusal never falls back to evade refusal.
        permanent_unavailable = failure.code in {"authentication", "credit_exhausted", "model_unavailable", "capability_unverified", "provider_configuration_invalid", "circuit_open"}
        if step.stage in {"intake", "synthesis", "audit"} and permanent_unavailable:
            options = run.configuration.get("fallbacks", {}).get(step.stage, [])
            for provider in options:
                if provider != step.provider and provider in {"openai", "anthropic", "google"} and (step.stage != "audit" or provider != synthesizer):
                    permitted = fits(0)
                    if permitted is None:
                        return Recovery(False, clock_regressed=True)
                    if not permitted:
                        return Recovery(False)
                    return Recovery(True, 0, provider)
        return Recovery(False)
    wait = failure.retry_after if failure.retry_after is not None else 2.5
    if not math.isfinite(wait) or wait < 0:
        wait = 2.5
    permitted = fits(wait)
    if permitted is None:
        return Recovery(False, clock_regressed=True)
    if not permitted:
        return Recovery(False)
    return Recovery(True, wait)
