import hashlib
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import Event, Meeting, Outbox, Owner, PolicyBucket, Run, utcnow


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def owner_lock(session: Session, owner_id: str) -> Owner:
    if session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    else:
        # A transaction-scoped lock serializes first-owner creation as well as commands.
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(hashlib.sha256(owner_id.encode()).digest()[:8], "big", signed=True)})
    owner = session.get(Owner, owner_id)
    if owner is None:
        owner = Owner(id=owner_id)
        session.add(owner)
        session.flush()
    return owner


def lock_policies(session: Session, owner_id: str):
    result = []
    for key in ("deployment", f"owner:{owner_id}"):
        dialect = session.bind.dialect.name
        if dialect in {"postgresql", "sqlite"}:
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            session.execute(insert(PolicyBucket).values(id=new_id("policy"), key=key, limit_usd="0", spending_locked=True, version=1, metadata_json={}).on_conflict_do_nothing(index_elements=["key"]))
        bucket = session.scalar(select(PolicyBucket).where(PolicyBucket.key == key).with_for_update().execution_options(populate_existing=True))
        if bucket is None:
            bucket = PolicyBucket(id=new_id("policy"), key=key, limit_usd="0", spending_locked=True, metadata_json={})
            session.add(bucket)
            session.flush()
        result.append(bucket)
    return result


def locked_run(session: Session, run_id: str) -> Run | None:
    return session.scalar(select(Run).where(Run.id == run_id).with_for_update().execution_options(populate_existing=True))


def append_event(session: Session, run: Run, event_type: str, payload: dict | None = None) -> Event:
    body = payload or {}
    if len(json.dumps(body, ensure_ascii=False).encode()) > 16384:
        raise ValueError("Event payload exceeds 16 KiB; store a record reference")
    run.event_sequence = (run.event_sequence or 0) + 1
    run.updated_at = utcnow()
    event = Event(id=new_id("event"), run_id=run.id, sequence=run.event_sequence, event_type=event_type, payload=body)
    session.add(event)
    return event


def enqueue(session: Session, run: Run, kind: str = "advance", step_id: str | None = None, payload: dict | None = None, due_at=None) -> Outbox:
    if kind == "advance":
        # Every caller holds the run/policy lock (or is creating this run).
        # An advance is an idempotent request to inspect the current run, not a
        # new timer generation. Keep its published record available for lost
        # delivery recovery instead of leaving a replayable predecessor behind
        # every time admission waits. Do not update it here: the publisher can
        # still hold that row while its successfully delivered task is running.
        existing = session.scalar(select(Outbox).where(
            Outbox.run_id == run.id, Outbox.kind == "advance",
            Outbox.status.in_(["pending", "published"]),
        ).order_by(Outbox.id).limit(1))
        if existing is not None:
            return existing
    body = payload or {"run_id": run.id}
    selected_due = due_at or utcnow()
    if kind == "execute":
        control = run.configuration.get("step_dispatch", {}).get(step_id, {})
        body = {**body, "dispatch_generation": control.get("generation", 0)}
        if control.get("not_before"):
            selected_due = max(selected_due, datetime.fromisoformat(control["not_before"]))
    work = Outbox(id=new_id("work"), run_id=run.id, step_id=step_id, kind=kind, status="pending", payload=body, due_at=selected_due)
    session.add(work)
    return work


def meeting_for_owner(session: Session, meeting_id: str, owner_id: str, *, lock=False) -> Meeting | None:
    query = select(Meeting).where(Meeting.id == meeting_id, Meeting.owner_id == owner_id)
    return session.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
