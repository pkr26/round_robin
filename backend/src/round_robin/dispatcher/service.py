"""Publish identifiers with at-least-once delivery; recover from database state."""

from datetime import timedelta
import os
import signal
import threading
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from round_robin.database.models import Attempt, BudgetOperation, Meeting, Outbox, PolicyBucket, Run, Step, utcnow
from round_robin.database.services import append_event, enqueue
from round_robin.database.session import SessionLocal
from round_robin.orchestration.saved_state import SavedStateUnreadable, read_saved_steps
from round_robin.orchestration.admission import TERMINAL, attempt_occupies_capacity, lock_work_run
from round_robin.orchestration.budget import BudgetDenied, aware, ledger_amount, reconcile_operation, release_unused_finish, remaining_active
from round_robin.orchestration.controller import advance_run, fence_unreadable_saved_state, finalize_cancelled_safely, finalize_run
from round_robin.worker.app import celery_app


def _send(kind, payload):
    celery_app.send_task(f"round_robin.{kind}", args=[payload], queue=os.environ.get("ROUND_ROBIN_TASK_QUEUE", "round_robin"), retry=False)


def _execute_scope_valid(session, run, step):
    if run is None or step is None or step.run_id != run.id:
        return False
    meeting = session.get(Meeting, run.meeting_id, populate_existing=True)
    return bool(meeting and meeting.owner_id == run.owner_id and meeting.deleted_at is None)


def dispatch_once(*, session_factory=SessionLocal, sender=None, limit=50):
    sender = sender or _send
    published = 0
    with session_factory() as session, session.begin():
        from round_robin.operations.durability import marker_pending
        if marker_pending(session=session):
            return 0
        deployment = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        if deployment and (deployment.metadata_json.get("dispatch_locked") or deployment.metadata_json.get("restore_quarantine")):
            return 0
        work = list(session.scalars(select(Outbox).where(Outbox.status == "pending", Outbox.kind.in_(["advance", "execute", "finalize", "research"]), Outbox.due_at <= utcnow()).order_by(Outbox.due_at, Outbox.id).limit(limit).with_for_update(skip_locked=True)))
        for item in work:
            if item.kind not in {"advance", "execute", "finalize", "research"}:
                # Export/storage services own their independent job families.
                continue
            if item.kind == "execute":
                run = session.get(Run, item.run_id, populate_existing=True)
                step = session.get(Step, item.step_id, populate_existing=True) if item.step_id else None
                if not _execute_scope_valid(session, run, step):
                    item.status = "cancelled"
                    continue
                current_generation = run.configuration.get("step_dispatch", {}).get(item.step_id, {}).get("generation", 0)
                if current_generation != item.payload.get("dispatch_generation", 0):
                    item.status = "done"
                    continue
            item.publication_attempts = (item.publication_attempts or 0) + 1
            try:
                argument = item.run_id
                if item.kind == "execute":
                    argument = {"step_id": item.step_id, "dispatch_generation": item.payload.get("dispatch_generation", 0)}
                sender(item.kind, argument)
            except Exception:
                # Redis failure leaves the original committed intention pending.
                item.due_at = utcnow() + timedelta(seconds=5)
                continue
            item.status, item.published_at = "published", utcnow()
            published += 1
    return published


def _validate_attempt_ledger(session, run, step, attempt):
    """Verify saved ownership before changing any attempt or liability."""
    from round_robin.providers.base import PROVIDERS
    if (step is None or step.run_id != run.id or attempt.run_id != run.id
            or attempt.step_id != step.id or attempt.provider not in PROVIDERS or attempt.provider != step.provider
            or (attempt.lease_owner is not None and step.lease_owner is not None and attempt.lease_owner != step.lease_owner)):
        raise BudgetDenied("attempt_identity_unavailable")
    ledger = session.scalar(select(BudgetOperation).where(BudgetOperation.id == attempt.id, BudgetOperation.run_id == run.id, BudgetOperation.owner_id == run.owner_id).with_for_update().execution_options(populate_existing=True))
    if (ledger is None or ledger.run_id != run.id or ledger.owner_id != run.owner_id or ledger.kind != "model"
            or ledger.status not in {"reserved", "unknown", "settled"}
            or ledger_amount(ledger.reserved_usd) != ledger_amount(attempt.reserved_usd)):
        raise BudgetDenied("attempt_identity_unavailable")
    if ledger.status == "settled":
        ledger_amount(ledger.settled_usd)


def _expire_step(session, run, step):
    step = session.scalar(select(Step).where(Step.id == step.id, Step.run_id == run.id).with_for_update().execution_options(populate_existing=True))
    if step is None or step.run_id != run.id:
        raise BudgetDenied("attempt_identity_unavailable")
    pending = (Attempt.step_id == step.id, Attempt.status == "authorized", Attempt.completed_at.is_(None))
    # Detect foreign attempts before taking their locks. They are ambiguous,
    # not evidence that this step is safe to requeue.
    if session.scalar(select(Attempt.id).where(*pending, Attempt.run_id != run.id).limit(1)) is not None:
        raise BudgetDenied("attempt_identity_unavailable")
    attempts = list(session.scalars(select(Attempt).where(*pending, Attempt.run_id == run.id).order_by(Attempt.id).with_for_update().execution_options(populate_existing=True)))
    # Validate the entire batch before changing any row.
    for attempt in attempts:
        _validate_attempt_ledger(session, run, step, attempt)
    if not attempts and step.status == "authorized":
        raise BudgetDenied("attempt_identity_unavailable")
    if attempts:
        # A replacement cannot know whether a prior executor sent. Never blindly
        # send that attempt again. Explicit eligible final-stage retries retain
        # original counters and need a separate new reservation.
        for attempt in attempts:
            attempt.status, attempt.usage_certainty = "unknown", "unknown"
            attempt.completed_at = utcnow()
            reconcile_operation(session, attempt.id, None, "unknown")
        step.status, step.error_code = "failed", "worker_lost_after_authorization"
        append_event(session, run, "attempt.outcome_unknown", {"step_id": step.id, "attempt_ids": [attempt.id for attempt in attempts]})
    elif step.infrastructure_claims < 3:
        step.status = "pending"
        enqueue(session, run, "execute", step_id=step.id)
    else:
        step.status, step.error_code = "failed", "infrastructure_claim_limit"
    step.lease_owner, step.lease_expires_at = None, None


def reconcile_once(*, session_factory=SessionLocal):
    changed = 0
    with session_factory() as session:
        run_ids = list(session.scalars(select(Run.id).where(Run.status.in_(["queued", "running", "awaiting_input", "cancel_requested"]))))
        orphan_ids = list(session.scalars(select(Attempt.id).join(Run, Run.id == Attempt.run_id).where(Run.status.in_(TERMINAL), Attempt.status == "authorized", Attempt.completed_at.is_(None))))
        retained_ids = list(session.scalars(select(Run.id).join(BudgetOperation, BudgetOperation.run_id == Run.id).where(
            Run.status.in_(TERMINAL), BudgetOperation.kind == "finish_hold", BudgetOperation.status == "held")))
    for run_id in retained_ids:
        try:
            with session_factory() as session, session.begin():
                run = lock_work_run(session, run_id)
                if not run or run.status not in TERMINAL:
                    continue
                from round_robin.orchestration.policies import retryable_steps
                hold = session.get(BudgetOperation, f"finish_{run_id}")
                if hold and hold.status == "held" and not run.configuration.get("saved_state_unavailable") and not retryable_steps(session, run):
                    # This is unused authorization, not an unresolved attempt.
                    # Unknown sent exposure remains in its separate ledger row.
                    if release_unused_finish(session, run):
                        append_event(session, run, "run.retry_reserve_released", {"reason": "retry_no_longer_eligible"})
                        changed += 1
        except (SQLAlchemyError, BudgetDenied):
            continue
    # A run may be cancelled or finalized while its worker is lost. Those runs
    # no longer enter the ordinary step reconciler, but their authorizations
    # still need an honest unknown outcome after the bounded executor lease.
    for attempt_id in orphan_ids:
        try:
            with session_factory() as session, session.begin():
                snapshot = session.get(Attempt, attempt_id)
                if not snapshot:
                    continue
                run = lock_work_run(session, snapshot.run_id)
                attempt = session.scalar(select(Attempt).where(Attempt.id == attempt_id).with_for_update().execution_options(populate_existing=True))
                if not run or not attempt or attempt.run_id != run.id or run.status not in TERMINAL or attempt.completed_at or attempt.status != "authorized" or attempt_occupies_capacity(session, attempt):
                    continue
                step = session.scalar(select(Step).where(Step.id == attempt.step_id, Step.run_id == run.id).with_for_update().execution_options(populate_existing=True))
                _validate_attempt_ledger(session, run, step, attempt)
                attempt.status, attempt.usage_certainty = "unknown", "unknown"
                attempt.completed_at = utcnow()
                reconcile_operation(session, attempt.id, None, "unknown")
                append_event(session, run, "attempt.outcome_unknown", {"step_id": attempt.step_id, "attempt_ids": [attempt.id]})
                changed += 1
        except (SQLAlchemyError, BudgetDenied):
            continue
    for run_id in run_ids:
        try:
            with session_factory() as session, session.begin():
                run = lock_work_run(session, run_id)
                if not run or run.status in TERMINAL:
                    continue
                previous_changed = changed
                try:
                    with session.begin_nested():
                        now = utcnow()
                        if run.status == "cancel_requested":
                            finalize_cancelled_safely(session, run)
                            changed += 1
                            continue
                        if run.status == "awaiting_input":
                            if run.input_expires_at and aware(run.input_expires_at) <= now:
                                finalize_run(session, run, reason="input_expired")
                                changed += 1
                            continue
                        for step in read_saved_steps(session, select(Step).where(Step.run_id == run.id, Step.status.in_(["claimed", "authorized"]))):
                            if step.lease_expires_at is None or aware(step.lease_expires_at) <= now:
                                _expire_step(session, run, step)
                                changed += 1
                        try:
                            expired = bool(run.first_admitted_at and remaining_active(run) <= 0)
                        except BudgetDenied as failure:
                            if str(failure) != "clock_regression":
                                raise
                            finalize_run(session, run, reason="clock_regression")
                            changed += 1
                            continue
                        if expired:
                            finalize_run(session, run, reason="active_deadline_exceeded")
                            changed += 1
                        else:
                            from round_robin.research.service import reconcile_public_research
                            reconcile_public_research(session, run)
                            advance_run(session, run)
                except SavedStateUnreadable:
                    changed = previous_changed
                    fence_unreadable_saved_state(session, run)
                    changed += 1
        except (SQLAlchemyError, BudgetDenied):
            # An unrelated run must not be blocked by unconfirmed work.
            continue
    with session_factory() as session, session.begin():
        seen_advances = set()
        for item in session.scalars(select(Outbox).where(Outbox.kind == "advance", Outbox.status.in_(["pending", "published"]))
                .order_by(Outbox.id).with_for_update(skip_locked=True)):
            # Repair previously accumulated duplicate timers while retaining
            # one recoverable intention. Advances always inspect current state.
            if item.run_id in seen_advances:
                item.status = "done"
            else:
                seen_advances.add(item.run_id)
        for item in session.scalars(select(Outbox).where(Outbox.status == "published", Outbox.kind.in_(["advance", "execute", "finalize", "research"])).with_for_update(skip_locked=True)):
            run = session.get(Run, item.run_id)
            if not run or run.status in TERMINAL:
                item.status = "done"
                continue
            if item.kind == "execute" and not item.step_id:
                item.status = "cancelled"
                continue
            if item.step_id:
                step = session.get(Step, item.step_id, populate_existing=True)
                if step is None:
                    item.status = "done"
                    continue
                if item.kind == "execute" and not _execute_scope_valid(session, run, step):
                    item.status = "cancelled"
                    continue
                current_generation = run.configuration.get("step_dispatch", {}).get(item.step_id, {}).get("generation", 0)
                if not step or step.status in {"authorized", "succeeded", "failed", "skipped"} or item.payload.get("dispatch_generation", 0) != current_generation:
                    item.status = "done"
                    continue
            elif run.status != "queued":
                item.status = "done"
                continue
            if item.published_at and aware(item.published_at) + timedelta(seconds=15) <= utcnow():
                item.status, item.due_at = "pending", utcnow()
                changed += 1
    return changed


def main():
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    last_maintenance = 0.0
    while not stopped.is_set():
        try:
            reconcile_once()
            dispatch_once()
            from round_robin.worker.image_tasks import dispatch_pending_image_operations, reconcile_image_operations
            reconcile_image_operations()
            dispatch_pending_image_operations()
        except SQLAlchemyError:
            pass
        if time.monotonic() - last_maintenance >= 60:
            try:
                from round_robin.operations.maintenance import maintenance
                from round_robin.configuration import Settings
                maintenance(SessionLocal, Settings().artifact_directory)
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Artifact cleanup unavailable; inspect the protected operator purge state.')
            last_maintenance = time.monotonic()
        stopped.wait(float(os.getenv("ROUND_ROBIN_DISPATCH_INTERVAL_SECONDS", "1")))


if __name__ == "__main__":
    main()
