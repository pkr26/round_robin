from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from round_robin.database.models import Attempt, BriefRevision, Command, DeletionRecord, Draft, Event, EvidenceRecord, ExportRecord, Meeting, Outcome, Outbox, PreflightRecord, ReportVersion, Run, Step, Upload, utcnow
from round_robin.database.services import append_event, enqueue, lock_policies, locked_run, new_id
from round_robin.schemas.application import CreateMeeting, DeleteMeeting, FollowUp, RecordOutcome, SaveDraft
from round_robin.schemas.views import DeletionView, DraftView, MeetingList, MeetingView, OutcomeList, OutcomeRecord, RunPage
from .auth import get_db, require_owner
from .commands import aware, command_begin, command_commit, fail, owned_meeting

router = APIRouter()


def draft_view(draft: Draft):
    return {"meeting_id": draft.meeting_id, "revision": draft.revision, "brief": draft.payload}


def meeting_view(session: Session, meeting: Meeting):
    from .events import run_view
    draft = session.get(Draft, meeting.id)
    runs = list(session.scalars(select(Run).where(Run.meeting_id == meeting.id, Run.owner_id == meeting.owner_id).order_by(Run.created_at.desc(), Run.id.desc()).limit(21)))
    return {"id": meeting.id, "title": meeting.title, "created_at": aware(meeting.created_at).isoformat(), "updated_at": aware(meeting.updated_at).isoformat(), "draft_revision": draft.revision, "draft": draft.payload, "runs": [run_view(session, run) for run in runs[:20]], "next_run_cursor": runs[19].id if len(runs) > 20 else None}


@router.post("/api/meetings", status_code=201, response_model=MeetingView)
def create_meeting(body: CreateMeeting, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "create_meeting", body.model_dump(mode="json"))
        if prior:
            return prior.result
        count = session.scalar(select(func.count()).select_from(Meeting).where(Meeting.owner_id == owner, Meeting.deleted_at.is_(None)))
        if count >= request.app.state.settings.max_meetings_per_owner:
            fail("quota_exceeded", "Meeting limit reached; explicitly delete an old meeting first", 429)
        meeting = Meeting(id=new_id("meeting"), owner_id=owner, title=body.brief.title)
        session.add(meeting)
        session.flush()
        payload = body.brief.model_dump(mode="json")
        payload["revision"] = 1
        session.add(Draft(meeting_id=meeting.id, revision=1, payload=payload))
        session.flush()
        result = meeting_view(session, meeting)
        command_commit(session, request, owner, body.command_id, "create_meeting", result, resource_id=meeting.id, meeting_id=meeting.id, status_code=201)
        return result


@router.get("/api/meetings", response_model=MeetingList)
def list_meetings(limit: int = 20, cursor: str | None = None, owner=Depends(require_owner), session: Session = Depends(get_db)):
    limit = min(50, max(1, limit))
    query = select(Meeting).where(Meeting.owner_id == owner, Meeting.deleted_at.is_(None))
    if cursor:
        query = query.where(Meeting.id > cursor)
    rows = list(session.scalars(query.order_by(Meeting.id).limit(limit + 1)))
    return {"items": [meeting_view(session, row) for row in rows[:limit]], "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


@router.get("/api/meetings/{meeting_id}", response_model=MeetingView)
def read_meeting(meeting_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    return meeting_view(session, owned_meeting(session, meeting_id, owner))


@router.get("/api/meetings/{meeting_id}/draft", response_model=DraftView)
def read_draft(meeting_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    owned_meeting(session, meeting_id, owner)
    return draft_view(session.get(Draft, meeting_id))


@router.get("/api/meetings/{meeting_id}/runs", response_model=RunPage)
def run_history(meeting_id: str, cursor: str | None = None, limit: int = 20, owner=Depends(require_owner), session: Session = Depends(get_db)):
    from .events import run_view
    owned_meeting(session, meeting_id, owner)
    limit = min(50, max(1, limit))
    query = select(Run).where(Run.meeting_id == meeting_id, Run.owner_id == owner)
    if cursor:
        pivot = session.get(Run, cursor)
        if pivot is None or pivot.meeting_id != meeting_id or pivot.owner_id != owner:
            fail("cursor_invalid", "Run-history cursor is outside this meeting", 400)
        query = query.where(or_(Run.created_at < pivot.created_at, and_(Run.created_at == pivot.created_at, Run.id < pivot.id)))
    rows = list(session.scalars(query.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)))
    return {"items": [run_view(session, run) for run in rows[:limit]], "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


@router.patch("/api/meetings/{meeting_id}/draft", response_model=DraftView)
@router.put("/api/meetings/{meeting_id}/draft", include_in_schema=False, response_model=DraftView)
def save_draft(meeting_id: str, body: SaveDraft, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        kind = "save_draft"
        prior = command_begin(session, request, owner, body.command_id, kind, {"meeting_id": meeting_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        meeting = owned_meeting(session, meeting_id, owner, lock=True)
        draft = session.get(Draft, meeting_id)
        if draft.revision != body.expected_revision:
            fail("draft_conflict", "The draft changed; reload and reconcile edits", retry_scope="read")
        payload = body.brief.model_dump(mode="json")
        payload["revision"] = draft.revision + 1
        draft.revision += 1
        draft.payload = payload
        meeting.title = body.brief.title
        draft.updated_at = meeting.updated_at = utcnow()
        result = draft_view(draft)
        command_commit(session, request, owner, body.command_id, kind, result, resource_id=meeting_id, meeting_id=meeting_id)
        return result


@router.post("/api/meetings/{meeting_id}/follow-ups", status_code=201, response_model=MeetingView)
def follow_up(meeting_id: str, body: FollowUp, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        kind = "follow_up"
        prior = command_begin(session, request, owner, body.command_id, kind, {"meeting_id": meeting_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        meeting = owned_meeting(session, meeting_id, owner, lock=True)
        parent = session.get(Run, body.parent_run_id)
        if not parent or parent.owner_id != owner or parent.meeting_id != meeting_id:
            fail("input_scope_invalid", "Follow-up parent must belong to the same meeting")
        # This writes only a new draft. An active parent's frozen context and
        # authority remain intact until an explicit replacement Start commits.
        draft = session.get(Draft, meeting_id)
        if draft.revision != body.expected_draft_revision:
            fail("draft_conflict", "The draft changed; reload and reconcile edits before creating a follow-up", retry_scope="read")
        draft.revision += 1
        payload = body.brief.model_dump(mode="json")
        payload["revision"] = draft.revision
        draft.payload, draft.parent_run_id = payload, parent.id
        meeting.title = body.brief.title
        meeting.updated_at = utcnow()
        session.flush()
        result = meeting_view(session, meeting)
        command_commit(session, request, owner, body.command_id, kind, result, resource_id=meeting_id, meeting_id=meeting_id, status_code=201)
        return result


@router.post("/api/meetings/{meeting_id}/outcomes", status_code=201, response_model=OutcomeRecord)
def record_outcome(meeting_id: str, body: RecordOutcome, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "record_outcome", {"meeting_id": meeting_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        owned_meeting(session, meeting_id, owner, lock=True)
        run = session.get(Run, body.run_id)
        if not run or run.meeting_id != meeting_id or run.owner_id != owner:
            fail("not_found", "Run not found", 404)
        # Recording the owner's observation requires the report identity, not
        # decoding or interpreting the immutable report content.
        report = session.scalar(select(ReportVersion.id).where(ReportVersion.run_id == run.id, ReportVersion.version == body.report_version))
        if not report:
            fail("report_pending", "The referenced saved report does not exist")
        version = (session.scalar(select(func.max(Outcome.revision)).where(Outcome.meeting_id == meeting_id)) or 0) + 1
        row = Outcome(id=new_id("outcome"), owner_id=owner, meeting_id=meeting_id, run_id=run.id, revision=version, payload=body.model_dump(mode="json", exclude={"command_id"}))
        session.add(row)
        session.flush()
        result = {"id": row.id, "revision": row.revision, "payload": row.payload, "created_at": row.created_at.isoformat()}
        command_commit(session, request, owner, body.command_id, "record_outcome", result, resource_id=row.id, meeting_id=meeting_id, status_code=201)
        return result


@router.get("/api/meetings/{meeting_id}/outcomes", response_model=OutcomeList)
def read_outcomes(meeting_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    owned_meeting(session, meeting_id, owner)
    from round_robin.reports.read_adapters import UnreadableReport
    from round_robin.schemas.views import OutcomePayload
    rows = session.scalars(select(Outcome).join(Run, Outcome.run_id == Run.id).where(
        Outcome.meeting_id == meeting_id, Outcome.owner_id == owner, Run.meeting_id == meeting_id, Run.owner_id == owner
    ).order_by(Outcome.revision))
    items = []
    for row in rows:
        try:
            if OutcomePayload.model_validate(row.payload).run_id != row.run_id:
                raise ValueError
        except (ValueError, TypeError):
            raise UnreadableReport("Saved outcome failed its run binding") from None
        items.append({"id": row.id, "revision": row.revision, "payload": row.payload, "created_at": aware(row.created_at).isoformat()})
    return {"items": items}


@router.delete("/api/meetings/{meeting_id}", response_model=DeletionView)
def delete_meeting(meeting_id: str, body: DeleteMeeting, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "delete_meeting", {"meeting_id": meeting_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        lock_policies(session, owner)
        meeting = owned_meeting(session, meeting_id, owner, lock=True)
        from round_robin.operations.deletion_scope import deletion_scope_consistent
        if not deletion_scope_consistent(session, meeting_id, owner):
            fail("deletion_scope_invalid", "Deletion requires recovery of inconsistent saved ownership", 409)
        # The root operations module supplies the independently durable deletion manifest.
        manifest = getattr(request.app.state, "record_deletion_manifest", None)
        if not callable(manifest):
            fail("deletion_manifest_unavailable", "Independent deletion tracking must be available before deletion", 503)
        manifest(meeting_id, owner, meeting.execution_fence + 1)
        meeting.deleted_at, meeting.title = utcnow(), ""
        meeting.execution_fence += 1
        session.add(DeletionRecord(meeting_id=meeting_id, owner_id=owner, execution_fence=meeting.execution_fence))
        runs = list(session.scalars(select(Run).where(Run.meeting_id == meeting_id).with_for_update()))
        run_ids = [run.id for run in runs]
        for run in runs:
            from round_robin.orchestration.budget import release_unused_finish
            release_unused_finish(session, run)
            run.execution_fence += 1
            run.version += 1
            run.status, run.stop_reason = "cancelled", "meeting_deleted"
            run.input_snapshot = {}
            run.configuration = {"deleted": True, "mode": run.configuration.get("mode", "simulated")}
        from round_robin.visual.service import purge_designs
        purge_designs(session, meeting_id, request.app.state.settings.artifact_directory)
        from round_robin.database.claim_models import ClaimVersion, ReuseRecord
        for model in (ClaimVersion, ReuseRecord, Event, EvidenceRecord, ReportVersion, BriefRevision):
            session.execute(delete(model).where(model.run_id.in_(run_ids)))
        for model in (Draft, Outcome):
            session.execute(delete(model).where(model.meeting_id == meeting_id))
        for upload in session.scalars(select(Upload).where(Upload.meeting_id == meeting_id)):
            upload.canonical_text, upload.filename, upload.status = None, "", "purged"
        for item in session.scalars(select(Outbox).where(Outbox.run_id.in_(run_ids))):
            item.status, item.payload = "cancelled", {}
        for row in session.scalars(select(PreflightRecord).where(PreflightRecord.meeting_id == meeting_id)):
            row.payload = {}
        for step in session.scalars(select(Step).where(Step.run_id.in_(run_ids))):
            step.result_json = None
        for attempt in session.scalars(select(Attempt).where(Attempt.run_id.in_(run_ids))):
            attempt.request_json, attempt.response_json, attempt.configuration = {}, None, {}
        session.execute(update(Command).where(Command.owner_id == owner, Command.meeting_id == meeting_id).values(gone=True, result={}))
        result = {"meeting_id": meeting_id, "access_revoked": True, "purge_status": "pending", "backup_erasure": "subject_to_configured_retention"}
        command_commit(session, request, owner, body.command_id, "delete_meeting", result, resource_id=meeting_id, meeting_id=meeting_id)
        return result
