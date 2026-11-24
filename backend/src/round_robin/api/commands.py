from datetime import timezone
import hashlib
import hmac
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from round_robin.database.models import Command, Meeting, Run
from round_robin.database.services import owner_lock
from round_robin.schemas.views import AbandonCommandView, CommandView
from .auth import get_db, require_owner

router = APIRouter()


def fail(code: str, message: str, status: int = 409, retry_scope: str = "none"):
    raise HTTPException(status, detail={"code": code, "message": message, "retry_scope": retry_scope, "command_outcome": "not_committed"})


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def meeting_receipt_scope_valid(session: Session, command: Command) -> bool:
    """Check the run identities in an old saved meeting view before disclosure."""
    if command.command_type not in {"create_meeting", "follow_up"}:
        return True
    result = command.result
    if not isinstance(result, dict) or not isinstance(command.meeting_id, str):
        return False
    if result.get("id") != command.meeting_id or command.resource_id != command.meeting_id:
        return False
    meeting = session.get(Meeting, command.meeting_id)
    if meeting is None or meeting.owner_id != command.owner_id or meeting.deleted_at is not None:
        return False
    runs = result.get("runs")
    if not isinstance(runs, list) or len(runs) > 100:
        return False
    if any(not isinstance(run, dict) or not isinstance(run.get("id"), str) or run.get("meeting_id") != meeting.id for run in runs):
        return False
    identities = {run["id"] for run in runs}
    cursor = result.get("next_run_cursor")
    if cursor is not None and (not isinstance(cursor, str) or cursor not in identities):
        return False
    if identities:
        scoped = set(session.scalars(select(Run.id).where(Run.id.in_(identities), Run.meeting_id == meeting.id, Run.owner_id == command.owner_id)))
        if scoped != identities:
            return False
    return True


def receipt_result(session: Session, command: Command) -> dict:
    from round_robin.visual.views import public_result
    if not meeting_receipt_scope_valid(session, command):
        return {"receipt_unavailable": True, "message": "The command committed, but its saved meeting result cannot be safely displayed."}
    return public_result(command.result)


def command_begin(session: Session, request: Request, owner_id: str, command_id, kind: str, body: dict) -> Command | None:
    owner_lock(session, owner_id)
    encoded = json.dumps({"kind": kind, "body": body}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    body_hash = hmac.new(request.app.state.settings.command_hash_key.encode(), encoded, hashlib.sha256).hexdigest()
    prior = session.get(Command, (owner_id, str(command_id)))
    if prior:
        if prior.command_type == "abandoned":
            fail("command_abandoned", "This uncommitted action was abandoned; its original UUID cannot be executed", 410)
        if not hmac.compare_digest(prior.body_hash, body_hash) or prior.command_type != kind:
            fail("command_conflict", "This command UUID was already used for a different operation")
        if prior.gone:
            fail("command_resource_gone", "The original command's meeting was deleted", 410)
        if not meeting_receipt_scope_valid(session, prior):
            raise HTTPException(409, detail={"code": "committed_receipt_unavailable", "message": "The command committed, but its saved meeting result cannot be safely displayed.", "retry_scope": "none", "command_outcome": "committed"})
        return prior
    request.state.command_hash = body_hash
    return None


def command_commit(session: Session, request: Request, owner: str, command_id, kind: str, result: dict, *, resource_id: str, meeting_id: str | None, status_code=200):
    session.add(Command(owner_id=owner, command_id=str(command_id), command_type=kind, body_hash=request.state.command_hash, resource_id=resource_id, meeting_id=meeting_id, result=result, status_code=status_code))


def owned_meeting(session: Session, meeting_id: str, owner_id: str, *, lock=False) -> Meeting:
    from round_robin.database.services import meeting_for_owner
    meeting = meeting_for_owner(session, meeting_id, owner_id, lock=lock)
    if meeting is None:
        fail("not_found", "Meeting not found", 404)
    if meeting.deleted_at:
        fail("command_resource_gone", "Meeting was deleted", 410)
    return meeting


@router.get("/api/commands/{command_id}", response_model=CommandView)
def read_command(command_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    command = session.get(Command, (owner, command_id))
    if command is None:
        fail("command_not_observed", "No committed command is currently visible; retry the original UUID to reconcile", 404, "original_command")
    return {"command_id": command.command_id, "command_type": command.command_type, "resource_id": command.resource_id, "status_code": 410 if command.gone else command.status_code, "result": {} if command.gone else receipt_result(session, command), "outcome": "abandoned" if command.command_type == "abandoned" else "gone" if command.gone else "committed"}


@router.post("/api/commands/{command_id}/abandon", response_model=AbandonCommandView)
def abandon_command(command_id: UUID, owner=Depends(require_owner), session: Session = Depends(get_db)):
    """Fence an unseen UUID atomically against an original request arriving late.

    Every command begins under this same owner lock. If its transaction won,
    return its saved outcome; otherwise the durable tombstone prevents any
    future execution, without retaining or asking for private input again.
    """
    with session.begin():
        owner_lock(session, owner)
        command = session.get(Command, (owner, str(command_id)))
        if command is None:
            command = Command(owner_id=owner, command_id=str(command_id), command_type="abandoned",
                body_hash="0" * 64, resource_id=None, meeting_id=None, result={}, status_code=410, gone=True)
            session.add(command)
        status = "abandoned" if command.command_type == "abandoned" else "gone" if command.gone else "committed"
        return {"status": status, "result": {} if command.gone else receipt_result(session, command),
            "command_type": command.command_type, "resource_id": command.resource_id}
