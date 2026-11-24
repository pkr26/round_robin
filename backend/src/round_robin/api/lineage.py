"""Explicit, atomic replacement authority for a linked follow-up Start."""
from round_robin.database.models import Run
from round_robin.database.services import append_event, enqueue, locked_run

from .commands import fail


ACTIVE = {"queued", "running", "awaiting_input", "cancel_requested"}


def _parent(session, draft, owner, *, lock=False):
    if not draft.parent_run_id:
        return None
    parent = locked_run(session, draft.parent_run_id) if lock else session.get(Run, draft.parent_run_id)
    if parent is None or parent.owner_id != owner or parent.meeting_id != draft.meeting_id:
        fail("input_scope_invalid", "Follow-up parent must belong to this owner and meeting")
    return parent


def capture_parent_replacement(session, draft, owner):
    parent = _parent(session, draft, owner)
    if parent is None or parent.status not in ACTIVE:
        return None
    return {"run_id": parent.id, "version": parent.version, "status": parent.status}


def replace_active_parent(session, draft, owner, accepted_payload, supplied_version):
    """Run inside the same owner/policy-locked transaction that creates a child.

    Saving a follow-up draft grants no cancellation authority. Only a current
    preflight and an explicit matching parent version permit replacement.
    Reservation, exposure and existing attempt accounting remain unchanged.
    """
    parent = _parent(session, draft, owner, lock=True)
    if accepted_payload is None:
        if supplied_version is not None or parent is not None and parent.status in ACTIVE:
            fail("parent_replacement_stale", "Review a new preflight before replacing the active parent", retry_scope="read")
        return
    if (not isinstance(accepted_payload, dict) or parent is None or accepted_payload.get("run_id") != parent.id
            or type(supplied_version) is not int or supplied_version != accepted_payload.get("version")
            or parent.version != supplied_version or parent.status != accepted_payload.get("status")
            or parent.status not in ACTIVE):
        fail("parent_replacement_stale", "Explicitly approve replacement of the current parent version in a fresh preflight", retry_scope="read")
    parent.status, parent.stop_reason = "cancel_requested", "replaced_by_follow_up"
    parent.version += 1
    parent.execution_fence += 1
    append_event(session, parent, "run.cancel_requested", {"reason": "replaced_by_follow_up", "message": "Parent execution fenced by an explicitly authorized linked Start"})
    enqueue(session, parent, "finalize")
