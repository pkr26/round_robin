from datetime import datetime, timedelta

from sqlalchemy import func, select

from round_robin.database.models import Attempt, BudgetOperation, Meeting, Run, Step, utcnow
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.database.services import append_event, lock_policies, locked_run
from .budget import BudgetDenied, active_elapsed, aware, checked_finish_hold, validate_run_ledger, remaining_active, reserve_finish


TERMINAL = {"completed", "completed_with_limitations", "failed", "cancelled"}
_UNKNOWN_VISUAL_EXPIRY = object()


def lock_work_run(session, run_id):
    snapshot = session.get(Run, run_id)
    if not snapshot:
        return None
    lock_policies(session, snapshot.owner_id)
    return locked_run(session, run_id)


def check_eligible(session, run):
    checked_finish_hold(session, run, lock=True)
    validate_run_ledger(list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id))), simulated=run.configuration.get("mode", "simulated") == "simulated")
    from .protocol import verify_policy
    if not verify_policy(run.configuration):
        raise BudgetDenied("discussion_policy_incompatible")
    from round_robin.operations.durability import marker_pending
    if marker_pending(session=session):
        raise BudgetDenied("durability_compromised")
    from .configuration import verify_frozen_briefs, verify_frozen_configuration, verify_saved_discussion
    verify_frozen_briefs(run)
    verify_saved_discussion(session, run)
    verify_frozen_configuration(run)
    meeting = session.get(Meeting, run.meeting_id)
    if not meeting or meeting.deleted_at:
        raise BudgetDenied("meeting_deleted")
    deployment, _ = lock_policies(session, run.owner_id)
    if deployment.metadata_json.get("dispatch_locked") or deployment.metadata_json.get("restore_quarantine"):
        raise BudgetDenied("dispatch_paused")
    if deployment.metadata_json.get("deployment_epoch", run.deployment_epoch) != run.deployment_epoch:
        raise BudgetDenied("execution_epoch_changed")
    if run.configuration.get("schema_version", "1.0") != "1.0":
        raise BudgetDenied("schema_incompatible")
    if run.status in TERMINAL or run.status == "cancel_requested":
        raise BudgetDenied("run_terminal")
    if run.first_admitted_at and remaining_active(run) <= 25:
        raise BudgetDenied("active_deadline_exceeded")


def ensure_queue_capacity(session, owner_id, *, owner_limit=3, run_id=None):
    """Use under the common policy lock for every transition into queued."""
    query = select(func.count()).select_from(Run).where(Run.status == "queued")
    if run_id is not None:
        query = query.where(Run.id != run_id)
    if session.scalar(query.where(Run.owner_id == owner_id)) >= owner_limit:
        raise BudgetDenied("owner_queue_limit")
    if session.scalar(query) >= 20:
        raise BudgetDenied("deployment_queue_limit")


def _visual_capacity_expiry(operation, design, meeting):
    """Return the active lease expiry, or reject an uninspectable record."""
    payload = operation.payload
    if design is None or not isinstance(payload, dict):
        raise BudgetDenied("capacity_state_invalid")
    status = payload.get("status")
    if status is None and payload.get("kind") == "review_supplement":
        return None
    if not isinstance(status, str):
        raise BudgetDenied("capacity_state_invalid")
    # A deleted session's purge tombstone no longer has the lease needed to
    # prove that an already-authorized or unknown call stopped running.
    if design.status == "deleted" and not isinstance(payload.get("kind"), str) and status in {"authorized", "unknown"}:
        if (operation.provider not in {"openai", "anthropic", "google"} or meeting is None
                or not design.owner_id or design.owner_id != meeting.owner_id):
            raise BudgetDenied("capacity_state_invalid")
        return _UNKNOWN_VISUAL_EXPIRY
    if status != "authorized":
        if status in {"queued", "blocked", "completed", "failed", "fenced", "reserved", "released", "settled", "unknown", "refused", "cancelled", "expired", "skipped"}:
            return None
        raise BudgetDenied("capacity_state_invalid")
    if (operation.provider not in {"openai", "anthropic", "google"} or meeting is None
            or not design.owner_id or design.owner_id != meeting.owner_id):
        raise BudgetDenied("capacity_state_invalid")
    try:
        return aware(datetime.fromisoformat(payload["lease_expires_at"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise BudgetDenied("capacity_state_invalid") from None


def capacity_delay_seconds(session):
    """Conservative occupied-capacity bound, not an availability prediction."""
    now = utcnow()
    delays = [0.0]
    for attempt in session.scalars(select(Attempt).where(Attempt.status == "authorized", Attempt.completed_at.is_(None))):
        try:
            expiry = _attempt_capacity_expiry(session, attempt)
        except BudgetDenied:
            return float("inf")
        delays.append(max(0.0, (expiry - now).total_seconds()))
    for operation, design, meeting in session.execute(select(DesignOperation, DesignSession, Meeting).outerjoin(DesignSession, DesignSession.id == DesignOperation.session_id).outerjoin(Meeting, Meeting.id == DesignSession.meeting_id)):
        try:
            expiry = _visual_capacity_expiry(operation, design, meeting)
        except BudgetDenied:
            return float("inf")
        if expiry is _UNKNOWN_VISUAL_EXPIRY:
            return float("inf")
        if expiry is not None:
            delays.append(max(0.0, (expiry - now).total_seconds()))
    return max(delays)


def admit_run(session, run) -> bool:
    check_eligible(session, run)
    if run.status == "running":
        return True
    if run.status != "queued":
        return False
    deployment, _ = lock_policies(session, run.owner_id)
    if deployment.metadata_json.get("draining"):
        return False
    now = utcnow()
    if run.first_admitted_at is None:
        if aware(run.initial_admission_expires_at) <= now:
            raise BudgetDenied("admission_expired")
    elif run.resumption_expires_at and aware(run.resumption_expires_at) <= now:
        raise BudgetDenied("resumption_expired")
    # The deployment policy lock serializes personal admission as well as spend.
    active = session.scalar(select(Run.id).where(Run.status == "running", Run.id != run.id).limit(1))
    if active:
        return False
    def ticket_key(candidate):
        # A clarification/retry joins the queue when its new 30-minute ticket
        # is issued; the original meeting's age must not jump the queue.
        issued = (aware(candidate.resumption_expires_at) - timedelta(minutes=30)
                  if candidate.first_admitted_at and candidate.resumption_expires_at
                  else aware(candidate.created_at))
        return issued, candidate.id

    current_ticket = ticket_key(run)
    for queued in session.scalars(select(Run).where(Run.status == "queued", Run.id != run.id)):
        expiry = queued.resumption_expires_at if queued.first_admitted_at else queued.initial_admission_expires_at
        meeting = session.get(Meeting, queued.meeting_id)
        if expiry and aware(expiry) > now and meeting and not meeting.deleted_at and ticket_key(queued) < current_ticket:
            return False
    resumed = run.first_admitted_at is not None
    run.status = "running"
    run.first_admitted_at = run.first_admitted_at or now
    run.active_started_at = now
    run.version += 1
    reserve_finish(session, run)
    append_event(session, run, "run.admitted", {"simulated": run.configuration.get("mode", "simulated") == "simulated", "active_seconds_remaining": remaining_active(run), "active_seconds": active_elapsed(run), "resumed": resumed})
    return True


def _attempt_step_scoped(attempt, step):
    from round_robin.providers.base import PROVIDERS
    if (step is None or step.run_id != attempt.run_id or not isinstance(attempt.provider, str)
            or attempt.provider not in PROVIDERS or step.provider != attempt.provider):
        return False
    return not (attempt.lease_owner is not None and step.lease_owner is not None and attempt.lease_owner != step.lease_owner)


def _attempt_capacity_expiry(session, attempt):
    """Never use an unbound step to shorten an attempt's capacity lease."""
    try:
        fallback = aware(attempt.authorized_at) + timedelta(seconds=150)
        step = session.get(Step, attempt.step_id, populate_existing=True)
        if _attempt_step_scoped(attempt, step) and step.lease_expires_at:
            return aware(step.lease_expires_at)
        return fallback
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise BudgetDenied("capacity_state_invalid") from None


def attempt_occupies_capacity(session, attempt, *, now=None):
    """Separate a bounded executor lease from unresolved monetary exposure.

    Finalization clears step leases to fence content, so a recently cancelled
    upstream call retains its original maximum 150-second lease. A lost worker
    cannot occupy execution capacity indefinitely after that lease expires.
    """
    return _attempt_capacity_expiry(session, attempt) > aware(now or utcnow())


def attempt_capacity_provider(session, attempt):
    """Return only an independently scoped, recognized provider identity."""
    step = session.get(Step, attempt.step_id, populate_existing=True)
    return attempt.provider if _attempt_step_scoped(attempt, step) else None


def provider_capacity_available(session, provider, *, mandatory=False):
    if not mandatory and session.scalar(select(Run.id).where(Run.status == "running", Run.stage.in_(["synthesis", "audit"])).limit(1)):
        # Existing calls retain their leases; new optional work cannot take the
        # slot a ready mandatory final stage is waiting to acquire.
        return False
    pending = [attempt for attempt in session.scalars(select(Attempt).where(Attempt.status == "authorized", Attempt.completed_at.is_(None))) if attempt_occupies_capacity(session, attempt)]
    if any(attempt_capacity_provider(session, attempt) is None for attempt in pending):
        return False
    images = []
    for operation, design, meeting in session.execute(select(DesignOperation, DesignSession, Meeting).outerjoin(DesignSession, DesignSession.id == DesignOperation.session_id).outerjoin(Meeting, Meeting.id == DesignSession.meeting_id)):
        expiry = _visual_capacity_expiry(operation, design, meeting)
        if expiry is _UNKNOWN_VISUAL_EXPIRY or expiry is not None and expiry > utcnow():
            images.append(operation)
    return len(pending) + len(images) < 3 and sum(attempt.provider == provider for attempt in pending + images) < 2
