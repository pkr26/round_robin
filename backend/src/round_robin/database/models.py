from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_fence: Mapped[int] = mapped_column(Integer, default=0)


class Draft(Base):
    __tablename__ = "drafts"
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    parent_run_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreflightRecord(Base):
    __tablename__ = "preflights"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(96), index=True)
    draft_revision: Mapped[int] = mapped_column(Integer)
    draft_hash: Mapped[str] = mapped_column(String(64))
    configuration_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_by_run_id: Mapped[str | None] = mapped_column(String(96), unique=True, nullable=True)


class Command(Base):
    __tablename__ = "commands"
    owner_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(64))
    body_hash: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    meeting_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    gone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(96), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    preflight_id: Mapped[str] = mapped_column(ForeignKey("preflights.id"), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(40), default="intake")
    version: Mapped[int] = mapped_column(Integer, default=1)
    deployment_epoch: Mapped[int] = mapped_column(Integer, default=1)
    execution_fence: Mapped[int] = mapped_column(Integer, default=0)
    recovery_generation: Mapped[int] = mapped_column(Integer, default=0)
    input_snapshot: Mapped[dict] = mapped_column(JSON)
    configuration: Mapped[dict] = mapped_column(JSON)
    initial_admission_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_seconds: Mapped[float] = mapped_column(default=0)
    active_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resumption_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    report_version: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("run_id", "stage", "round", "stage_iteration", "role_slot_id", name="uq_logical_step"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    round: Mapped[int] = mapped_column(Integer, default=0)
    stage_iteration: Mapped[int] = mapped_column(Integer, default=0)
    role_slot_id: Mapped[str] = mapped_column(String(96))
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    calls: Mapped[int] = mapped_column(Integer, default=0)
    infrastructure_claims: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(96), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BriefRevision(Base):
    __tablename__ = "brief_revisions"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_brief_revision"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    command_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(40), default="authorized")
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorization_epoch: Mapped[int] = mapped_column(Integer, default=1)
    recovery_generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(96), nullable=True)
    reserved_usd: Mapped[str] = mapped_column(String(64), default="0")
    settled_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage_certainty: Mapped[str] = mapped_column(String(32), default="reserved")
    request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_event_sequence"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), default="advance")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_attempts: Mapped[int] = mapped_column(Integer, default=0)


class ReportVersion(Base):
    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_report_version"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    event_cutoff: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outcome(Base):
    __tablename__ = "outcomes"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    owner_id: Mapped[str] = mapped_column(String(96))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(32), default="temporary")
    filename: Mapped[str] = mapped_column(String(256))
    media_type: Mapped[str] = mapped_column(String(128))
    canonical_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    recipients: Mapped[list] = mapped_column(JSON, default=lambda: ["openai", "anthropic", "google"])
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceRecord(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(String(96), index=True)
    owner_id: Mapped[str] = mapped_column(String(96))
    content: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExportRecord(Base):
    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(96))
    report_version: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyBucket(Base):
    __tablename__ = "policy_buckets"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    limit_usd: Mapped[str] = mapped_column(String(64), default="0")
    spending_locked: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class BudgetOperation(Base):
    __tablename__ = "budget_operations"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    owner_id: Mapped[str] = mapped_column(String(96), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    reserved_usd: Mapped[str] = mapped_column(String(64))
    settled_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class DeletionRecord(Base):
    __tablename__ = "deletion_records"
    meeting_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(96))
    execution_fence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
