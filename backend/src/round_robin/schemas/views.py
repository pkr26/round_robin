"""Typed serialized application views, distinct from fictional sample artifacts."""
from datetime import date
import hashlib
from typing import Any, Literal, Self

from pydantic import AwareDatetime, ConfigDict, Field, model_validator
from round_robin.providers.base import IntakeChange

from .briefs import DraftBrief, PublicTerm
from .common import Contract, Hash, Id, ShortText
from .preflight import FinishPolicy, Limits
from .reports import Readiness
from .runtime import RuntimeDecisionReport


class RemainingLimits(Contract):
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    requests: int | None = Field(ge=0, le=24)
    requests_unavailable_reason: Literal["ledger_integrity_failed"] | None = None
    active_seconds: float = Field(ge=0, le=600)


class RunView(Contract):
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    report_unavailable_reason: Literal["saved_state_integrity_failed"] | None = Field(default=None, exclude_if=lambda value: value is None)
    id: Id
    meeting_id: Id
    parent_run_id: Id | None
    status: Literal["queued", "running", "awaiting_input", "cancel_requested", "cancelled", "completed", "completed_with_limitations", "failed"]
    stage: ShortText
    version: int = Field(ge=1)
    stop_reason: ShortText | None
    created_at: AwareDatetime
    initial_admission_expires_at: AwareDatetime
    first_admitted_at: AwareDatetime | None
    active_seconds: float = Field(ge=0)
    report_version: int = Field(ge=0)
    execution_mode: Literal["simulated", "live"]
    remaining_limits: RemainingLimits
    readiness: Readiness | None


class MeetingView(Contract):
    id: Id
    title: str = Field(max_length=200)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    draft_revision: int = Field(ge=1)
    draft: DraftBrief
    runs: list[RunView] = Field(max_length=100)
    next_run_cursor: Id | None = None


class RunPage(Contract):
    items: list[RunView] = Field(max_length=50)
    next_cursor: Id | None


class MeetingList(Contract):
    items: list[MeetingView] = Field(max_length=50)
    next_cursor: Id | None


class DraftView(Contract):
    meeting_id: Id
    revision: int = Field(ge=1)
    brief: DraftBrief


class SelectedInputView(Contract):
    id: Id
    sha256: Hash
    recipients: list[Literal["openai", "anthropic", "google"]] = Field(min_length=3, max_length=3)
    byte_size: int = Field(ge=0, le=1048576)


class PublicResearchView(Contract):
    recipient: Literal["Brave Search"] | None
    approved_public_region: str = Field(max_length=120)
    enabled: bool
    query_templates: list[Literal["comparison_v1", "costs_v1", "counterevidence_v1"]] = Field(max_length=3)


class PreflightView(Contract):
    id: Id
    meeting_id: Id
    draft_revision: int = Field(ge=1)
    draft_hash: Hash
    configuration_hash: Hash
    fingerprint: Hash
    input_manifest: list[SelectedInputView] = Field(max_length=5)
    reuse_manifest: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    parent_replacement: dict[str, Any] | None = None
    parent_run_id: Id | None = None
    recipients: list[Literal["openai", "anthropic", "google"]] = Field(min_length=3, max_length=3)
    approved_public_terms: list[PublicTerm] = Field(max_length=8)
    public_research: PublicResearchView
    protocol: Literal["independent_review_provisional"]
    mode: Literal["simulated", "live"]
    limits: Limits
    finish_policy: FinishPolicy
    configuration: dict[str, Any]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    initial_admission_window_minutes: Literal[30]
    consumed_by_run_id: Id | None
    blockers: list[ShortText] = Field(max_length=20)
    warnings: list[ShortText] = Field(max_length=20)
    paid_access_enabled: bool


class EventView(Contract):
    run_id: Id
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=80)
    schema_version: Literal["1.0"]
    timestamp: AwareDatetime
    payload: dict[str, Any]


class EventPage(Contract):
    items: list[EventView] = Field(max_length=200)
    event_cutoff: int = Field(ge=0)
    next_cursor: int = Field(ge=0)


class StoredEvidence(Contract):
    id: Id
    run_id: Id
    owner_id: Id
    source_title: str = Field(min_length=1, max_length=2048)
    text: str = Field(min_length=1, max_length=2000)
    sha256: Hash
    origin: Literal["user_supplied", "retrieved"]
    verification_status: Literal["user_supplied", "not_checked", "supported", "mixed", "contradicted"]
    verified_at: AwareDatetime
    time_sensitive: bool
    origin_group: Id
    locator: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=1000)
    original_alias: Id | None = None
    input_id: Id | None = None
    url: str | None = Field(default=None, max_length=2048)
    source_content_hash: Hash | None = None
    source_text_length: int | None = Field(default=None, ge=1)
    selected_start: int | None = Field(default=None, ge=0)
    selected_end: int | None = Field(default=None, ge=1)
    selection_method: str | None = Field(default=None, max_length=80)
    uninspected_characters: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def exact_passage_hash(self) -> Self:
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("Passage hash must match its exact stored UTF-8 text")
        if self.selected_start is not None or self.selected_end is not None:
            if self.selected_start is None or self.selected_end is None or self.source_text_length is None:
                raise ValueError("Selected source span requires complete offsets and source length")
            if not 0 <= self.selected_start < self.selected_end <= self.source_text_length or self.selected_end - self.selected_start != len(self.text):
                raise ValueError("Selected passage offsets must match its exact character span")
        return self


class Snapshot(Contract):
    run: RunView
    brief: DraftBrief | None
    report: RuntimeDecisionReport | None
    evidence: list[StoredEvidence] | None = Field(max_length=24)
    events: list[EventView] = Field(max_length=100)
    event_cutoff: int = Field(ge=0)
    next_event_cursor: int | None = Field(default=None, ge=0)
    questions: list[str] = Field(max_length=3)
    proposed_changes: list[IntakeChange] = Field(default_factory=list, max_length=5)
    retry_eligible: bool
    retryable_stages: list[Literal["synthesis", "audit"]] = Field(max_length=2)
    change_summary: list[str] = Field(max_length=20)


class MessageView(Contract):
    id: Id
    stage: ShortText
    provider: Literal["openai", "anthropic", "google"] | None
    role: ShortText
    contribution: dict[str, Any]


class MessagePage(Contract):
    items: list[MessageView] = Field(max_length=50)
    next_cursor: Id | None


class OutcomePayload(Contract):
    run_id: Id
    report_version: int = Field(ge=1)
    choice: Literal["accepted", "modified", "deferred", "rejected"]
    reason: str = Field(max_length=2000)
    success_criterion: str = Field(max_length=500)
    observed_outcome: str = Field(max_length=4000)
    revisit_date: date | None


class OutcomeRecord(Contract):
    id: Id
    revision: int = Field(ge=1)
    payload: OutcomePayload
    created_at: AwareDatetime


class OutcomeList(Contract):
    items: list[OutcomeRecord]


class CommandView(Contract):
    command_id: str = Field(min_length=36, max_length=36)
    command_type: ShortText
    resource_id: Id | None
    status_code: int = Field(ge=200, le=599)
    result: dict[str, Any]
    outcome: Literal["committed", "gone", "abandoned"]


class AbandonCommandView(Contract):
    status: Literal["abandoned", "committed", "gone"]
    result: dict[str, Any]
    command_type: ShortText
    resource_id: Id | None


class DeletionView(Contract):
    meeting_id: Id
    access_revoked: Literal[True]
    purge_status: Literal["pending", "purged", "failed"]
    backup_erasure: Literal["subject_to_configured_retention"]
