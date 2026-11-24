import asyncio
from datetime import timedelta
import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from round_robin.database.models import Attempt, BriefRevision, BudgetOperation, Event, EvidenceRecord, Meeting, PolicyBucket, ReportVersion, Run, Step, utcnow
from round_robin.database.services import append_event, enqueue, lock_policies, locked_run, new_id
from round_robin.orchestration.policies import retryable_steps
from round_robin.schemas.application import Clarification, RetryStage, RunCommand
from round_robin.schemas.views import EventPage, MessagePage, RunView, Snapshot
from round_robin.schemas.runtime import RuntimeDecisionReport
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report, read_stored_evidence
from .auth import get_db, require_owner
from .commands import aware, command_begin, command_commit, fail, owned_meeting

router = APIRouter()
TERMINAL = {"completed", "completed_with_limitations", "cancelled", "failed"}


def owned_run(session: Session, run_id: str, owner: str, *, lock=False) -> Run:
    run = locked_run(session, run_id) if lock else session.get(Run, run_id)
    if run is None or run.owner_id != owner:
        fail("not_found", "Run not found", 404)
    owned_meeting(session, run.meeting_id, owner)
    return run


def ledger_state(session: Session, run: Run) -> tuple[bool, int | None]:
    from round_robin.orchestration.budget import BudgetDenied, checked_finish_hold, validate_run_ledger
    if run.configuration.get("ledger_integrity_failed") is True:
        return True, None
    try:
        checked_finish_hold(session, run)
        calls = validate_run_ledger(list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id))), simulated=run.configuration.get("mode", "simulated") == "simulated")
    except BudgetDenied:
        return True, None
    return False, calls


def ledger_unavailable(session: Session, run: Run) -> bool:
    return ledger_state(session, run)[0]


def latest_report(session: Session, run: Run, *, accounting=None):
    accounting = accounting if accounting is not None else ledger_state(session, run)
    if accounting[0] or run.configuration.get("saved_state_unavailable") is True:
        return None
    try:
        return session.scalar(select(ReportVersion).where(ReportVersion.run_id == run.id).order_by(ReportVersion.version.desc()).limit(1))
    except (ValueError, RecursionError):
        raise UnreadableReport("Stored report could not be decoded") from None


def run_view(session: Session, run: Run, *, accounting=None):
    accounting = accounting if accounting is not None else ledger_state(session, run)
    try:
        report = latest_report(session, run, accounting=accounting)
    except UnreadableReport:
        report = None
    readiness = None
    if report is not None:
        try:
            readiness = read_runtime_report(session, report)["readiness"]
        except UnreadableReport:
            # Keep run state and Stop inspectable, but never certify damaged
            # saved support using the report's unchecked readiness field.
            pass
    step_calls = sum(session.scalars(select(Step.calls).where(Step.run_id == run.id)))
    if accounting[0]:
        requests, requests_unavailable_reason = None, "ledger_integrity_failed"
    else:
        requests = max(0, run.configuration.get("limits", {}).get("max_requests", 24) - max(step_calls, accounting[1]))
        requests_unavailable_reason = None
    active = run.active_seconds + (max(0, (utcnow() - aware(run.active_started_at)).total_seconds()) if run.active_started_at else 0)
    return {"report_unavailable_reason": "saved_state_integrity_failed" if run.configuration.get("saved_state_unavailable") is True else None, "id": run.id, "meeting_id": run.meeting_id, "parent_run_id": run.parent_run_id, "status": run.status, "stage": run.stage, "version": run.version, "stop_reason": run.stop_reason, "created_at": aware(run.created_at).isoformat(), "initial_admission_expires_at": aware(run.initial_admission_expires_at).isoformat(), "first_admitted_at": aware(run.first_admitted_at).isoformat() if run.first_admitted_at else None, "active_seconds": active, "report_version": run.report_version, "execution_mode": run.configuration.get("mode", "simulated"), "remaining_limits": {"requests": requests, "requests_unavailable_reason": requests_unavailable_reason, "active_seconds": 0 if run.configuration.get("active_clock_invalid") else max(0, run.configuration.get("limits", {}).get("active_seconds", 600) - active)}, "readiness": readiness}


def event_view(event: Event):
    return {"run_id": event.run_id, "sequence": event.sequence, "event_type": event.event_type, "schema_version": event.schema_version, "timestamp": aware(event.created_at).isoformat(), "payload": event.payload}


@router.get("/api/runs/{run_id}", response_model=RunView)
def read_run(run_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    return run_view(session, owned_run(session, run_id, owner))


@router.get("/api/runs/{run_id}/snapshot", response_model=Snapshot)
def snapshot(run_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner, lock=True)
    accounting = ledger_state(session, run)
    report = latest_report(session, run, accounting=accounting)
    events = list(session.scalars(select(Event).where(Event.run_id == run_id, Event.sequence <= run.event_sequence).order_by(Event.sequence).limit(100)))
    from round_robin.orchestration.budget import BudgetDenied
    from round_robin.orchestration.configuration import verify_frozen_briefs
    try:
        verify_frozen_briefs(run)
        brief = None if run.configuration.get("invalid_frozen_inputs") is True else run.configuration.get("resolved_brief", run.input_snapshot)
    except BudgetDenied:
        brief = None
    retry_stages = sorted({step.stage for step in retryable_steps(session, run)}) if brief is not None else []
    saved_report = read_runtime_report(session, report) if report else None
    evidence_unavailable = bool(
        saved_report
        and run.configuration.get("comparison_unavailable_reason") == "evidence_integrity_failed"
        and saved_report["execution_status"] in {"failed", "cancelled", "completed_with_limitations"}
        and run.status == saved_report["execution_status"]
        and run.stop_reason == saved_report["stop_reason"]
        and report.version == run.report_version
        and saved_report["readiness"] is None
        and not saved_report["comparison"]
        and not saved_report["evidence_ids"]
        and "Stored evidence could not be validated and is unavailable in this report." in saved_report["main_reasons"]
    )
    evidence = None
    if not evidence_unavailable:
        try:
            evidence = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(EvidenceRecord.id).limit(24)))
        except (ValueError, RecursionError):
            if not accounting[0] and run.configuration.get("saved_state_unavailable") is not True:
                raise UnreadableReport("Stored evidence could not be decoded") from None
    try:
        evidence_view = None if evidence_unavailable or evidence is None else [read_stored_evidence(item, run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id) for item in evidence]
    except UnreadableReport:
        if not accounting[0] and run.configuration.get("saved_state_unavailable") is not True:
            raise
        # Keep the stopped run readable without presenting damaged evidence.
        evidence_view = None
    return {"run": run_view(session, run, accounting=accounting), "brief": brief, "report": saved_report, "evidence": evidence_view, "events": [event_view(item) for item in events], "event_cutoff": run.event_sequence, "next_event_cursor": events[-1].sequence if len(events) == 100 else None, "questions": run.configuration.get("questions", []), "proposed_changes": run.configuration.get("proposed_changes", []), "retry_eligible": bool(retry_stages), "retryable_stages": retry_stages, "change_summary": report.content.get("change_summary", []) if report else []}


@router.get("/api/runs/{run_id}/events", response_model=EventPage)
def read_events(run_id: str, after: int = 0, limit: int = 100, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner)
    cursor = min(run.event_sequence, max(0, after))
    # READ COMMITTED permits a writer to commit between loading the run and
    # loading events. Keep this page bounded to the sequence we actually read.
    events = list(session.scalars(select(Event).where(Event.run_id == run_id, Event.sequence > cursor, Event.sequence <= run.event_sequence).order_by(Event.sequence).limit(min(200, max(1, limit)))))
    return {"items": [event_view(item) for item in events], "event_cutoff": run.event_sequence, "next_cursor": events[-1].sequence if events else cursor}


@router.get("/api/runs/{run_id}/stream")
async def stream_events(run_id: str, request: Request, after: int = 0, last_event_id: str | None = Header(default=None), owner=Depends(require_owner)):
    cursor = max(0, after)
    if last_event_id:
        try:
            prefix, value = last_event_id.rsplit(":", 1)
            if prefix != run_id:
                fail("event_cursor_invalid", "Event cursor belongs to another run", 400)
            parsed_cursor = int(value)
            if parsed_cursor < 0 or parsed_cursor > 2**63 - 1:
                fail("event_cursor_invalid", "Invalid event cursor", 400)
            cursor = max(cursor, parsed_cursor)
        except ValueError:
            fail("event_cursor_invalid", "Invalid event cursor", 400)
    def current_cutoff():
        with request.app.state.session_factory() as session:
            return owned_run(session, run_id, owner).event_sequence

    cursor = min(cursor, await run_in_threadpool(current_cutoff))

    async def generate():
        nonlocal cursor

        def delivery_allowed():
            identity_provider = getattr(request.app.state, "identity_provider", None)
            if identity_provider is not None:
                from fastapi import HTTPException
                try:
                    if identity_provider(request) != owner:
                        return False
                except HTTPException:
                    return False
            with request.app.state.session_factory() as session:
                deployment = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
                run = session.get(Run, run_id)
                meeting = session.get(Meeting, run.meeting_id) if run else None
                return bool(run and run.owner_id == owner and meeting and not meeting.deleted_at
                            and not (deployment and (deployment.metadata_json.get("restore_quarantine")
                                or deployment.metadata_json.get("deployment_epoch", run.deployment_epoch) != run.deployment_epoch)))

        def read_batch():
            with request.app.state.session_factory() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return None
                # Materialize a bounded batch before delivery; never hold a DB
                # transaction open across a slow or disconnected browser.
                events = [event_view(event) for event in session.scalars(select(Event).where(Event.run_id == run_id, Event.sequence > cursor, Event.sequence <= run.event_sequence).order_by(Event.sequence).limit(100))]
                return events, run.status in TERMINAL, run.event_sequence

        while not await request.is_disconnected():
            if not await run_in_threadpool(delivery_allowed):
                return
            batch = await run_in_threadpool(read_batch)
            if batch is None:
                return
            events, terminal, cutoff = batch
            for event in events:
                if await request.is_disconnected() or not await run_in_threadpool(delivery_allowed):
                    return
                cursor = event["sequence"]
                yield f"id: {run_id}:{cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
            if terminal and cursor >= cutoff:
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/api/runs/{run_id}/messages", response_model=MessagePage)
def messages(run_id: str, cursor: str | None = None, limit: int = 20, owner=Depends(require_owner), session: Session = Depends(get_db)):
    owned_run(session, run_id, owner)
    limit = min(50, max(1, limit))
    query = select(Step).where(Step.run_id == run_id, Step.result_json.is_not(None))
    if cursor:
        query = query.where(Step.id > cursor)
    rows = list(session.scalars(query.order_by(Step.id).limit(limit + 1)))
    visible = rows[:limit]
    return {"items": [{"id": row.id, "stage": row.stage, "provider": row.provider, "role": row.role_slot_id, "contribution": row.result_json} for row in visible], "next_cursor": visible[-1].id if len(rows) > limit else None}


@router.get("/api/runs/{run_id}/reports/{version}", response_model=RuntimeDecisionReport)
def report(run_id: str, version: int, owner=Depends(require_owner), session: Session = Depends(get_db)):
    owned_run(session, run_id, owner)
    try:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id, ReportVersion.version == version))
    except (ValueError, RecursionError):
        raise UnreadableReport("Stored report could not be decoded") from None
    if row is None:
        fail("report_pending", "Saved report version not found", 404, "read")
    return read_runtime_report(session, row)


@router.get("/api/runs/{run_id}/report", response_model=RuntimeDecisionReport)
def current_report(run_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner)
    row = latest_report(session, run)
    if row is None:
        if ledger_unavailable(session, run):
            fail("ledger_integrity_failed", "The current report and usage are unavailable because saved accounting records could not be verified.", 503, "read")
        if run.configuration.get("saved_state_unavailable") is True:
            fail("saved_state_integrity_failed", "The current report is unavailable because saved state could not be verified.", 503, "read")
        fail("report_pending", "A saved report is not available yet", 404, "read")
    return read_runtime_report(session, row)


def retry_eligibility(session: Session, run: Run) -> bool:
    return bool(retryable_steps(session, run))


def run_view_remaining_active(run: Run):
    if run.configuration.get("active_clock_invalid"):
        return 0
    elapsed = run.active_seconds + (max(0, (utcnow() - aware(run.active_started_at)).total_seconds()) if run.active_started_at else 0)
    return max(0, run.configuration.get("limits", {}).get("active_seconds", 600) - elapsed)


@router.post("/api/runs/{run_id}/cancel", response_model=RunView)
def cancel(run_id: str, body: RunCommand, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "cancel", {"run_id": run_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        lock_policies(session, owner)
        run = owned_run(session, run_id, owner, lock=True)
        if run.version != body.expected_run_version:
            fail("run_conflict", "Run version changed; reload before cancelling", retry_scope="read")
        if run.status not in TERMINAL:
            run.status, run.stop_reason = "cancel_requested", "user_cancellation"
            run.version += 1
            run.execution_fence += 1
            append_event(session, run, "run.cancel_requested", {"message": "Cancellation committed; no new calls may be authorized"})
            enqueue(session, run, "finalize")
        elif not run.configuration.get("saved_state_unavailable") and not ledger_unavailable(session, run) and (hold := session.get(BudgetOperation, f"finish_{run.id}")) is not None and hold.status == "held" and not run.configuration.get("retry_abandoned"):
            from round_robin.orchestration.budget import release_unused_finish
            if release_unused_finish(session, run):
                run.configuration = {**run.configuration, "retry_abandoned": True}
                run.version += 1
                run.execution_fence += 1
                append_event(session, run, "run.retry_abandoned", {"message": "Unused retry funds released; the saved report and unknown attempt exposure are preserved"})
        session.flush()
        result = run_view(session, run)
        command_commit(session, request, owner, body.command_id, "cancel", result, resource_id=run.id, meeting_id=run.meeting_id)
        return result


@router.post("/api/runs/{run_id}/clarifications", response_model=RunView)
def clarify(run_id: str, body: Clarification, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "clarify", {"run_id": run_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        lock_policies(session, owner)
        run = owned_run(session, run_id, owner, lock=True)
        if run.status != "awaiting_input" or run.version != body.expected_run_version:
            fail("clarification_stale", "This input request is no longer current")
        if run.input_expires_at and aware(run.input_expires_at) <= utcnow():
            fail("input_expired", "The input wait expired; create a linked follow-up")
        if not 2 <= sum(option.role == "candidate" for option in body.brief.options) <= 4 or body.brief.horizon_months is None:
            fail("brief_incomplete", "Confirm two to four candidates and a common horizon before proposals")
        if body.brief.selected_attachment_ids != run.input_snapshot.get("selected_attachment_ids", []) or [item.model_dump() for item in body.brief.public_research_terms] != run.input_snapshot.get("public_research_terms", []):
            fail("input_scope_invalid", "Clarification cannot add attachments or change approved public scope")
        from .preflight import input_packet_error
        packet_error = input_packet_error(session, run.meeting_id, owner, body.brief.model_dump(mode="json"),
            run.configuration.get("input_manifest", []), run.configuration.get("reuse_manifest", []))
        if packet_error:
            fail(*packet_error)
        from round_robin.orchestration.admission import ensure_queue_capacity
        from round_robin.orchestration.budget import BudgetDenied
        try:
            ensure_queue_capacity(session, owner, owner_limit=request.app.state.settings.max_queued_per_owner, run_id=run.id)
        except BudgetDenied:
            fail("quota_exceeded", "Queue is full; the confirmation remains available within its original expiry", 429, "original_command")
        run.input_snapshot = body.brief.model_dump(mode="json")
        from sqlalchemy import func
        brief_version = (session.scalar(select(func.max(BriefRevision.version)).where(BriefRevision.run_id == run.id)) or 0) + 1
        session.add(BriefRevision(id=new_id("brief"), run_id=run.id, version=brief_version, kind="confirmed", payload=run.input_snapshot, command_id=str(body.command_id)))
        run.configuration = {**run.configuration, "questions": [], "proposed_changes": [], "confirmed_by_command": str(body.command_id)}
        run.version += 1
        run.status, run.stage = "queued", "proposals"
        run.resumption_expires_at = utcnow() + timedelta(minutes=30)
        run.input_expires_at = None
        append_event(session, run, "brief.confirmed", {"revision": body.brief.revision})
        enqueue(session, run)
        result = run_view(session, run)
        command_commit(session, request, owner, body.command_id, "clarify", result, resource_id=run.id, meeting_id=run.meeting_id)
        return result


@router.post("/api/runs/{run_id}/retries", response_model=RunView)
def retry(run_id: str, body: RetryStage, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "retry", {"run_id": run_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        lock_policies(session, owner)
        run = owned_run(session, run_id, owner, lock=True)
        if run.version != body.expected_run_version:
            fail("retry_ineligible", "This stage cannot be retried within its original limits")
        step = next((item for item in retryable_steps(session, run) if item.stage == body.stage), None)
        if step is None:
            fail("retry_ineligible", "Requested stage has no eligible failed execution")
        from round_robin.orchestration.admission import ensure_queue_capacity
        from round_robin.orchestration.budget import BudgetDenied
        try:
            ensure_queue_capacity(session, owner, owner_limit=request.app.state.settings.max_queued_per_owner, run_id=run.id)
        except BudgetDenied:
            fail("quota_exceeded", "Queue is full; retry remains subject to its original limits", 429, "original_command")
        run.recovery_generation += 1
        run.version += 1
        run.status, run.stage = "queued", body.stage
        run.resumption_expires_at = utcnow() + timedelta(minutes=30)
        step.status, step.generation = "pending", run.recovery_generation
        step.lease_owner, step.lease_expires_at = None, None
        controls = dict(run.configuration.get("step_dispatch", {}))
        control = controls.get(step.id, {})
        controls[step.id] = {**control, "generation": control.get("generation", 0) + 1, "not_before": utcnow().isoformat()}
        run.configuration = {**run.configuration, "step_dispatch": controls}
        append_event(session, run, "stage.retry_requested", {"stage": body.stage, "recovery_generation": run.recovery_generation})
        enqueue(session, run)
        result = run_view(session, run)
        command_commit(session, request, owner, body.command_id, "retry", result, resource_id=run.id, meeting_id=run.meeting_id)
        return result
