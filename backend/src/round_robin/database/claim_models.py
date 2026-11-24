"""Immutable server-issued claim versions and explicit child reuse provenance."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utcnow


class ClaimVersion(Base):
    __tablename__ = "claim_versions"
    __table_args__ = (UniqueConstraint("run_id", "step_id", "alias", name="uq_step_claim_alias"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(96), index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("steps.id"), nullable=True)
    alias: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReuseRecord(Base):
    __tablename__ = "reuse_records"
    __table_args__ = (UniqueConstraint("run_id", "parent_id", name="uq_run_parent_reuse"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(96), index=True)
    parent_run_id: Mapped[str] = mapped_column(String(96))
    parent_id: Mapped[str] = mapped_column(String(96))
    parent_version: Mapped[int] = mapped_column(Integer)
    parent_sha256: Mapped[str] = mapped_column(String(64))
    child_id: Mapped[str] = mapped_column(String(96), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
