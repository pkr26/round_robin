"""One upstream call per task, with immutable authorization committed first."""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import threading
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from round_robin.database.models import Attempt, BudgetOperation, DeletionRecord, EvidenceRecord, Meeting, Run, Step, utcnow
from round_robin.database.services import append_event, enqueue, new_id
from round_robin.database.session import SessionLocal
from round_robin.orchestration.saved_state import SavedStateUnreadable, read_saved_step, read_saved_steps
from round_robin.orchestration.admission import capacity_delay_seconds, check_eligible, lock_work_run, provider_capacity_available
from round_robin.orchestration.budget import BudgetDenied, active_elapsed, amount, authorize_operation, aware, reconcile_operation, remaining_active
from round_robin.orchestration.controller import advance_run, finalize_run, intake_evidence_view, validated_evidence
from round_robin.orchestration.circuits import before_attempt, record_outcome
from round_robin.orchestration.policies import plan_recovery
from round_robin.providers.base import OUTPUT_TOKENS, ProviderFailure, ProviderRequest
from round_robin.providers.http import configured_provider
from .app import celery_app


def _saved_packet_digest(value):
    """Compare JSON identity without exposing packet contents in diagnostics."""
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
        return None
    return hashlib.sha256(encoded).digest()


def build_request(session, run, step):
    brief = run.configuration.get("resolved_brief", run.input_snapshot)
    evidence = validated_evidence(session, run)
    if step.stage == "intake" and not run.configuration.get("evidence_prepared"):
        brief, evidence = intake_evidence_view(session, run)
    from round_robin.research.reuse import claim_packet
    try:
        claims = claim_packet(session, run, imported_only=step.stage == "proposal")
    except ValueError:
        raise BudgetDenied("claim_integrity_failed") from None
    peers = []
    if step.stage in {"synthesis", "audit"}:
        prior_steps = list(session.scalars(select(Step).where(Step.run_id == run.id, Step.status == "succeeded").order_by(Step.stage_iteration, Step.created_at, Step.id)))
        selected = [prior for prior in prior_steps if prior.stage == "proposal"]
        for stage in (["synthesis"] if step.stage == "audit" else ["synthesis", "audit"] if step.stage_iteration > 0 else []):
            latest = next((prior for prior in reversed(prior_steps) if prior.stage == stage), None)
            if latest is not None:
                selected.append(latest)
        peers = [{"id": prior.id, "provider": prior.provider, "stage": prior.stage, "output": prior.result_json["output"]} for prior in selected]
    from round_robin.orchestration.reporting import comparison_as_of, comparison_data
    from round_robin.schemas.briefs import DraftBrief
    cost_as_of = comparison_as_of(run)
    _, calculations = comparison_data(DraftBrief.model_validate(brief), as_of=cost_as_of)
    references = {record["id"] for record in evidence} | {calculation["id"] for calculation in calculations} | {peer["id"] for peer in peers} | {claim["id"] for claim in claims}
    from round_robin.orchestration.protocol import mandatory_tail_seconds
    finish_time = mandatory_tail_seconds(step.stage, run.configuration.get("finish_policy", {}).get("reserved_active_seconds", 190))
    timeout = min(90.0, remaining_active(run) - finish_time - 25)
    if timeout <= 0:
        raise BudgetDenied("finish_time_reserve")
    from round_robin.research.discussion import assessment_packet, open_objection_packet
    issues = [] if step.stage in {"proposal", "intake"} else open_objection_packet(session, run)
    return ProviderRequest(run.id, step.id, step.stage, step.role_slot_id, brief, evidence, peers, frozenset(references), OUTPUT_TOKENS.get(step.stage, 2048), timeout, step.calls > 0 and step.error_code in {"schema_invalid", "malformed_response", "invalid_reference"}, tuple(calculations), tuple(claims), cost_as_of=cost_as_of.isoformat() if cost_as_of else None,
        repair_diagnostics=tuple(run.configuration.get("step_dispatch", {}).get(step.id, {}).get("repair_diagnostics", [])),
        open_objections=tuple(issues), assessments=tuple(assessment_packet(session, run)))


def _settle_failure(session, run, step, failure):
    step.error_code = failure.code
    synthesizer = session.scalar(select(Step.provider).where(Step.run_id == run.id, Step.stage == "synthesis", Step.status == "succeeded").order_by(Step.stage_iteration.desc()).limit(1))
    logical_steps = read_saved_steps(session, select(Step).where(Step.run_id == run.id, Step.stage == step.stage, Step.round == step.round, Step.role_slot_id == step.role_slot_id))
    recovery = plan_recovery(run, step, failure, synthesizer=synthesizer, logical_calls=sum(prior.calls for prior in logical_steps), logical_claims=sum(prior.infrastructure_claims for prior in logical_steps), capacity_delay_seconds=capacity_delay_seconds(session))
    step.lease_owner, step.lease_expires_at = None, None
    if recovery.retry:
        step.status = "pending"
        due = utcnow() + timedelta(seconds=recovery.delay_seconds)
        controls = dict(run.configuration.get("step_dispatch", {}))
        old = controls.get(step.id, {})
        diagnostics = getattr(failure, "diagnostics", ())
        controls[step.id] = {"generation": old.get("generation", 0) + 1, "not_before": due.isoformat(),
                             "repair_diagnostics": list(diagnostics)[:12] or [{"code": failure.code, "path": "$"}]}
        run.configuration = {**run.configuration, "step_dispatch": controls}
        if recovery.fallback_provider:
            original = step.provider
            step.provider = recovery.fallback_provider
            append_event(session, run, "role.reassigned", {"step_id": step.id, "original_provider": original, "provider": step.provider, "reason": failure.code})
        enqueue(session, run, "execute", step_id=step.id, due_at=due)
        append_event(session, run, "step.retry_scheduled", {"step_id": step.id, "error_code": failure.code, "calls_used": step.calls, "retry_after_seconds": recovery.delay_seconds})
    else:
        step.status = "failed"
        append_event(session, run, "step.failed", {"step_id": step.id, "stage": step.stage, "provider": step.provider, "error_code": failure.code})
        if recovery.clock_regressed:
            # The current interval cannot be measured honestly even if wall
            # time catches up before finalization. Retain only counted time.
            run.active_started_at = None
            run.configuration = {**run.configuration, "active_clock_invalid": True}
            finalize_run(session, run, reason="clock_regression")
        else:
            advance_run(session, run)


class _StepIdentityChanged(Exception):
    """A post-send step no longer has the authorization-bound identity."""


def _heartbeat(step_id, token, session_factory, stop):
    while not stop.wait(10):
        try:
            with session_factory() as session, session.begin():
                snapshot = read_saved_step(session, select(Step).where(Step.id == step_id))
                if not snapshot:
                    return
                run = lock_work_run(session, snapshot.run_id)
                if not run:
                    return
                step = read_saved_step(session, select(Step).where(Step.id == step_id, Step.run_id == run.id).with_for_update().execution_options(populate_existing=True))
                if not step or step.lease_owner != token or step.status != "authorized" or run.status != "running":
                    return
                check_eligible(session, run)
                step.lease_expires_at = utcnow() + timedelta(seconds=150)
        except (SQLAlchemyError, BudgetDenied, SavedStateUnreadable):
            # Failure to prove authority must not extend a lease.
            return


def execute_step(step_id: str, *, session_factory=SessionLocal, adapter_registry=None, dispatch_generation=None):
    token, attempt_id = new_id("lease"), new_id("attempt")
    provider = request = None
    try:
        with session_factory() as session, session.begin():
            snapshot = read_saved_step(session, select(Step).where(Step.id == step_id))
            if snapshot is None:
                return {"status": "missing"}
            run = lock_work_run(session, snapshot.run_id)
            if not run:
                return {"status": "settled"}
            step = read_saved_step(session, select(Step).where(Step.id == step_id, Step.run_id == run.id).with_for_update().execution_options(populate_existing=True))
            if not step:
                return {"status": "scope_unavailable"}
            if step.status in {"succeeded", "failed", "skipped"}:
                return {"status": "settled"}
            if step.status in {"claimed", "authorized"}:
                return {"status": "already_claimed"}
            control = run.configuration.get("step_dispatch", {}).get(step.id, {})
            if dispatch_generation is not None and dispatch_generation != control.get("generation", 0):
                return {"status": "stale_dispatch_generation"}
            if control.get("not_before") and aware(datetime.fromisoformat(control["not_before"])) > utcnow():
                return {"status": "retry_wait", "not_before": control["not_before"]}
            try:
                check_eligible(session, run)
                if run.status != "running":
                    return {"status": "admission_wait"}
                if step.generation != run.recovery_generation:
                    return {"status": "stale_generation"}
                if step.stage_iteration != run.configuration.get("stage_iteration", 0):
                    return {"status": "stale_iteration"}
                expected_stage = "proposal" if run.stage == "proposals" else run.stage
                if expected_stage != step.stage:
                    return {"status": "wrong_stage"}
                if step.stage == "proposal" and run.configuration.get("research_state") in {"pending", "running"}:
                    enqueue(session, run, "execute", step_id=step.id, due_at=utcnow() + timedelta(seconds=2))
                    return {"status": "research_wait"}
                logical_role_steps = list(session.scalars(select(Step).where(Step.run_id == run.id, Step.stage == step.stage, Step.round == step.round, Step.role_slot_id == step.role_slot_id)))
                if sum(prior.infrastructure_claims for prior in logical_role_steps) >= 3:
                    raise BudgetDenied("infrastructure_claim_limit")
                if not provider_capacity_available(session, step.provider, mandatory=step.stage in {"synthesis", "audit"}):
                    enqueue(session, run, "execute", step_id=step.id, due_at=utcnow() + timedelta(seconds=2))
                    return {"status": "capacity_wait"}
                if step.stage == "audit":
                    synthesizer = session.scalar(select(Step.provider).where(Step.run_id == run.id, Step.stage == "synthesis", Step.status == "succeeded", Step.stage_iteration == step.stage_iteration).limit(1))
                    if synthesizer is None or step.provider == synthesizer:
                        raise BudgetDenied("independent_auditor_unavailable")
                step.infrastructure_claims += 1
                step.status, step.lease_owner = "claimed", token
                step.lease_expires_at = utcnow() + timedelta(seconds=150)
                request = build_request(session, run, step)
                provider = adapter_registry[step.provider] if adapter_registry is not None else configured_provider(step.provider, run.configuration)
                bound = provider.conservative_bound(request)
                # A denied authorization must not commit a phantom half-open
                # probe or any partial ledger changes made while checking it.
                with session.begin_nested():
                    before_attempt(session, run, step.provider)
                    authorize_operation(session, run, attempt_id, "model", bound, step=step, finish_role=step.stage if step.stage in {"synthesis", "audit"} else None)
                step.status = "authorized"
                authorized_request = deepcopy(request.packet())
                authorized_request_digest = _saved_packet_digest(authorized_request)
                if authorized_request_digest is None:
                    raise BudgetDenied("invalid_saved_request")
                authorized_configuration = {"execution_fence": run.execution_fence, "model": provider.model, "mode": run.configuration.get("mode", "simulated"), "pricing_version": run.configuration.get("pricing", {}).get(step.provider, {}).get("version")}
                authorized_configuration_digest = _saved_packet_digest(authorized_configuration)
                if authorized_configuration_digest is None:
                    raise BudgetDenied("invalid_saved_request")
                session.add(Attempt(id=attempt_id, run_id=run.id, step_id=step.id, provider=step.provider, status="authorized", authorization_epoch=run.deployment_epoch, recovery_generation=run.recovery_generation, lease_owner=token, reserved_usd=str(bound), usage_certainty="reserved", request_json=authorized_request, configuration=authorized_configuration))
                authorization_identity = (run.id, run.owner_id, step.id, step.provider, str(bound), run.execution_fence, run.deployment_epoch, run.recovery_generation, token)
                append_event(session, run, "attempt.authorized", {"attempt_id": attempt_id, "step_id": step.id, "provider": step.provider, "reserved_usd": str(bound), "calls_used": step.calls})
            except ProviderFailure as failure:
                _settle_failure(session, run, step, failure)
                return {"status": "provider_blocked", "code": failure.code}
            except BudgetDenied as exc:
                if run.status == "cancel_requested":
                    from round_robin.orchestration.controller import finalize_cancelled_safely
                    finalize_cancelled_safely(session, run)
                    return {"status": "cancelled"}
                if str(exc) == "dispatch_paused":
                    return {"status": "dispatch_paused"}
                if step.stage == "proposal" and str(exc) in {"finish_call_reserve", "finish_time_reserve", "run_budget_exhausted"}:
                    step.status, step.error_code = "skipped", str(exc)
                    step.lease_owner, step.lease_expires_at = None, None
                    append_event(session, run, "step.skipped", {"step_id": step.id, "stage": step.stage, "provider": step.provider, "error_code": str(exc)})
                    advance_run(session, run)
                    return {"status": "policy_skipped", "code": str(exc)}
                step.status, step.error_code = "failed", str(exc)
                step.lease_owner, step.lease_expires_at = None, None
                finalize_run(session, run, reason=str(exc))
                return {"status": "policy_blocked", "code": str(exc)}
        # COMMIT has acknowledged immutable authorization before this boundary.
    except SavedStateUnreadable:
        return {"status": "saved_state_unavailable"}
    except SQLAlchemyError:
        return {"status": "authorization_unconfirmed"}
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(step_id, token, session_factory, stop), daemon=True)
    result, failure = None, None
    heartbeat_started = False
    try:
        try:
            heartbeat.start()
            heartbeat_started = True
        except RuntimeError:
            raise ProviderFailure("worker_heartbeat_unavailable", may_have_sent=False)
        result = provider.generate(request)
    except ProviderFailure as exc:
        failure = exc
    except BaseException as exc:
        # Soft task limits/unclassified adapter errors do not imply a free call.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        failure = ProviderFailure("unclassified_provider_failure")
    finally:
        stop.set()
        if heartbeat_started:
            heartbeat.join(timeout=1)
    started = time.monotonic()
    for persistence_try in range(3):
        try:
            with session_factory() as session, session.begin():
                snapshot = session.get(Attempt, attempt_id)
                if snapshot is None or snapshot.run_id != authorization_identity[0]:
                    return {"status": "persistence_unconfirmed", "attempt_id": attempt_id}
                run = lock_work_run(session, snapshot.run_id)
                step_identity = session.execute(select(Step.id, Step.run_id).where(Step.id == step_id, Step.run_id == authorization_identity[0]).with_for_update()).one_or_none()
                attempt = session.scalar(select(Attempt).where(Attempt.id == attempt_id).with_for_update().execution_options(populate_existing=True))
                ledger = session.scalar(select(BudgetOperation).where(BudgetOperation.id == attempt_id).with_for_update().execution_options(populate_existing=True))
                run_id, owner_id, saved_step_id, saved_provider, saved_bound, fence, epoch, generation, lease = authorization_identity
                meeting = session.get(Meeting, run.meeting_id) if run else None
                deletion = session.get(DeletionRecord, run.meeting_id) if run else None
                deleted_redacted = bool(
                    run and meeting and deletion and meeting.owner_id == owner_id and meeting.deleted_at
                    and deletion.owner_id == owner_id and deletion.meeting_id == meeting.id
                    and deletion.execution_fence == meeting.execution_fence and deletion.status in {"pending", "purged"}
                    and run.status == "cancelled" and run.stop_reason == "meeting_deleted"
                    and run.execution_fence > fence and isinstance(run.configuration, dict) and run.configuration.get("deleted") is True
                    and attempt and attempt.request_json == {} and attempt.configuration == {} and attempt.response_json is None
                )
                if (not run or run.id != run_id or run.owner_id != owner_id or not step_identity or step_identity.id != saved_step_id or step_identity.run_id != run_id
                        or not attempt or attempt.run_id != run_id or attempt.step_id != saved_step_id or attempt.provider != saved_provider
                        or attempt.reserved_usd != saved_bound or attempt.authorization_epoch != epoch
                        or attempt.recovery_generation != generation or attempt.lease_owner != lease
                        or (not deleted_redacted and (_saved_packet_digest(attempt.configuration) != authorized_configuration_digest
                            or attempt.configuration.get("execution_fence") != fence or _saved_packet_digest(attempt.request_json) != authorized_request_digest))
                        or not ledger or ledger.run_id != run_id or ledger.owner_id != owner_id or ledger.kind != "model"
                        or ledger.reserved_usd != saved_bound or ledger.status not in {"reserved", "unknown", "settled"}):
                    return {"status": "persistence_unconfirmed", "attempt_id": attempt_id}
                cost = result.cost_usd if result else failure.cost_usd
                certainty = "reported" if cost is not None else "proven_not_sent" if failure and not failure.may_have_sent else "unknown"
                if attempt.completed_at:
                    if attempt.usage_certainty == "unknown" and certainty != "unknown":
                        reconcile_operation(session, attempt_id, cost, certainty)
                        attempt.settled_usd, attempt.usage_certainty = str(cost) if cost is not None else "0", certainty
                        attempt.provider_request_id = result.provider_request_id if result else failure.provider_request_id
                        return {"status": "late_usage_reconciled"}
                    return {"status": "already_persisted"}
                reconcile_operation(session, attempt_id, cost, certainty)
                attempt.settled_usd = str(cost) if cost is not None else "0" if certainty == "proven_not_sent" else None
                attempt.usage_certainty = certainty
                attempt.provider_request_id = result.provider_request_id if result else failure.provider_request_id
                attempt.completed_at = utcnow()
                attempt.status = "succeeded" if result else "unknown" if certainty == "unknown" else "failed"
                if deleted_redacted:
                    # Deletion intentionally erased the packet and config. The
                    # remaining committed identity supports usage only; never
                    # restore deleted request, response or contribution data.
                    return {"status": "late_result_fenced"}
                try:
                    with session.begin_nested():
                        step = read_saved_step(session, select(Step).where(Step.id == saved_step_id, Step.run_id == run_id).with_for_update().execution_options(populate_existing=True))
                        if step is None:
                            raise _StepIdentityChanged()
                        policy_eligible = True
                        invalid_saved_state = False
                        denial_reason = None
                        try:
                            check_eligible(session, run)
                        except BudgetDenied as error:
                            denial_reason = str(error)
                            invalid_saved_state = denial_reason in {"invalid_frozen_inputs", "discussion_integrity_failed", "ledger_integrity_failed", "finish_money_reserve"}
                            policy_eligible = False
                        except AttributeError:
                            policy_eligible = False
                        try:
                            eligible = bool(policy_eligible and run and meeting and not meeting.deleted_at and run.status == "running" and step.lease_owner == token and step.stage_iteration == run.configuration.get("stage_iteration", 0) and step.generation == run.recovery_generation == attempt.recovery_generation and run.deployment_epoch == attempt.authorization_epoch and run.execution_fence == attempt.configuration["execution_fence"] and step.lease_expires_at and aware(step.lease_expires_at) > utcnow() and remaining_active(run) > 0)
                        except BudgetDenied as error:
                            if str(error) != "clock_regression":
                                raise
                            eligible, denial_reason = False, "clock_regression"
                        content_active = None
                        if eligible and result:
                            try:
                                content_active = active_elapsed(run)
                            except BudgetDenied as error:
                                if str(error) != "clock_regression":
                                    raise
                                eligible, denial_reason = False, "clock_regression"
                        if denial_reason == "clock_regression" and run and run.status in {"running", "cancel_requested"}:
                            # The already reconciled receipt must commit even when a
                            # later wall-clock read is untrustworthy. Discard content,
                            # fence future work and never schedule a fallback.
                            run.active_started_at = None
                            run.configuration = {**run.configuration, "active_clock_invalid": True}
                            step.status, step.error_code = "failed", failure.code if failure else "clock_regression"
                            step.lease_owner, step.lease_expires_at = None, None
                            attempt.response_json = {"error_code": failure.code} if failure else {"content_discarded": True, "usage": result.usage}
                            append_event(session, run, "step.failed", {"step_id": step.id, "stage": step.stage, "provider": step.provider, "error_code": step.error_code})
                            finalize_run(session, run, reason=None if run.status == "cancel_requested" else "clock_regression")
                            return {"status": "persisted", "attempt_id": attempt_id}
                        if not eligible:
                            # Late content is not retained on a deleted or fenced run.
                            attempt.response_json = {"content_discarded": True, "usage": result.usage if result and meeting and not meeting.deleted_at else {}}
                            if invalid_saved_state and run and run.status in {"running", "cancel_requested"}:
                                finalize_run(session, run, reason=None if run.status == "cancel_requested" else denial_reason)
                            return {"status": "late_result_fenced"}
                        if result:
                            from round_robin.research.reuse import persist_claims
                            from round_robin.research.claims import ClaimRejected
                            try:
                                # Claims and discussion obligations are one contribution.
                                # A rejected obligation cannot leave orphan claim rows.
                                with session.begin_nested():
                                    committed_output = persist_claims(session, run, step, result.output.model_dump(mode="json"), allowed_reference_ids=set(request.allowed_reference_ids))
                                    from round_robin.research.discussion import persist_discussion
                                    committed_output = persist_discussion(session, run, step, committed_output, allowed_reference_ids=set(request.allowed_reference_ids))
                            except ClaimRejected:
                                failure = ProviderFailure("claim_integrity_failed", cost_usd=result.cost_usd, provider_request_id=result.provider_request_id)
                                result = None
                                attempt.status = "unknown" if certainty == "unknown" else "failed"
                        if result:
                            record_outcome(session, run, step.provider)
                            step.status, step.error_code = "succeeded", None
                            step.result_json = {"output": committed_output, "usage": result.usage, "simulated": result.simulated, "actual_provider": result.actual_provider, "actual_model": result.actual_model, "attempt_id": attempt_id}
                            attempt.response_json = step.result_json
                            step.lease_owner, step.lease_expires_at = None, None
                            qualifying = bool(not result.simulated and step.stage in {"proposal", "synthesis"} and any(constraint.basis_refs and constraint.reason for constraint in result.output.constraints))
                            append_event(session, run, "content.committed", {"step_id": step.id, "stage": step.stage, "provider": result.actual_provider, "simulated": result.simulated, "active_seconds": content_active, "qualifying_content": qualifying})
                            advance_run(session, run)
                        else:
                            record_outcome(session, run, step.provider, failure)
                            attempt.response_json = {"error_code": failure.code}
                            _settle_failure(session, run, step, failure)
                except SavedStateUnreadable:
                    from round_robin.orchestration.controller import fence_unreadable_saved_state
                    fence_unreadable_saved_state(session, run)
                    safe_codes = {"refusal", "authentication", "credit_exhausted", "model_unavailable", "rate_limit", "transient", "provider_rejected_request", "connection_failure", "timeout", "transport_failure", "malformed_response", "schema_invalid", "invalid_reference", "response_limit", "truncated", "unclassified_provider_failure"}
                    provider_code = failure.code if failure and failure.code in safe_codes else "provider_failure" if failure else None
                    attempt.response_json = {"content_discarded": True, "error_code": "saved_state_integrity_failed", "provider_error_code": provider_code}
                    return {"status": "receipt_persisted_state_unavailable", "attempt_id": attempt_id}
            return {"status": "persisted", "attempt_id": attempt_id}
        except _StepIdentityChanged:
            return {"status": "persistence_unconfirmed", "attempt_id": attempt_id}
        except SQLAlchemyError:
            if persistence_try == 2 or time.monotonic() - started >= 18:
                return {"status": "persistence_unconfirmed", "attempt_id": attempt_id}
            time.sleep(min(0.25 * (persistence_try + 1), max(0, 18 - (time.monotonic() - started))))
    return {"status": "persistence_unconfirmed"}


@celery_app.task(name="round_robin.execute", acks_late=True)
def execute_task(step_id):
    if isinstance(step_id, dict):
        return execute_step(step_id["step_id"], dispatch_generation=step_id["dispatch_generation"])
    return execute_step(step_id)


@celery_app.task(name="round_robin.advance", acks_late=True)
def advance_task(run_id):
    try:
        with SessionLocal() as session, session.begin():
            run = lock_work_run(session, run_id)
            if run:
                try:
                    with session.begin_nested():
                        advance_run(session, run)
                except SavedStateUnreadable:
                    from round_robin.orchestration.controller import fence_unreadable_saved_state
                    fence_unreadable_saved_state(session, run)
        return {"status": "advanced"}
    except SQLAlchemyError:
        # Celery must not log SQL parameter bodies containing private briefs.
        return {"status": "database_outcome_unconfirmed"}


@celery_app.task(name="round_robin.finalize", acks_late=True)
def finalize_task(run_id):
    try:
        with SessionLocal() as session, session.begin():
            run = lock_work_run(session, run_id)
            if run:
                if run.status == "cancel_requested":
                    from round_robin.orchestration.controller import finalize_cancelled_safely
                    finalize_cancelled_safely(session, run)
                else:
                    try:
                        with session.begin_nested():
                            finalize_run(session, run)
                    except SavedStateUnreadable:
                        from round_robin.orchestration.controller import fence_unreadable_saved_state
                        fence_unreadable_saved_state(session, run)
        return {"status": "finalized"}
    except SQLAlchemyError:
        return {"status": "database_outcome_unconfirmed"}
