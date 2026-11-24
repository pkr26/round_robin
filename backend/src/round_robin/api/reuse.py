"""Owner-scoped immutable claim reads for explicit follow-up selection."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from round_robin.research.reuse import ClaimRejected, claim_packet
from .auth import get_db, require_owner
from .commands import fail
from .events import owned_run

router = APIRouter()


@router.get("/api/runs/{run_id}/claims")
def claims(run_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    run = owned_run(session, run_id, owner)
    try:
        return {"items": claim_packet(session, run)}
    except ClaimRejected:
        fail("claim_unreadable", "Saved claims could not be safely read; their records are preserved", 409)
