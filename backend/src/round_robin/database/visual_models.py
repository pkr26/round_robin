"""Optional visual persistence; importing registers metadata without enabling work."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utcnow


class DesignSession(Base):
    __tablename__ = "design_sessions"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(96), index=True)
    mode: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    execution_fence: Mapped[int] = mapped_column(Integer, default=0)
    deployment_epoch: Mapped[int] = mapped_column(Integer, default=1)
    frozen_brief: Mapped[dict] = mapped_column(JSON)
    recipients: Mapped[list] = mapped_column(JSON, default=list)
    maximum_usd: Mapped[str] = mapped_column(String(64), default="0")
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    preflight: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    selected_version_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    selection_recorded: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DesignVersion(Base):
    __tablename__ = "design_versions"
    __table_args__ = (UniqueConstraint("session_id", "version", name="uq_design_version"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("design_sessions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    original_hash: Mapped[str] = mapped_column(String(64))
    asset_path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    generator_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(24))
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DesignReview(Base):
    __tablename__ = "design_reviews"
    __table_args__ = (UniqueConstraint("version_id", "provider", name="uq_design_critic"),)
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("design_sessions.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("design_versions.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    receipt: Mapped[dict] = mapped_column(JSON)
    critique: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DesignOperation(Base):
    __tablename__ = "design_operations"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("design_sessions.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
