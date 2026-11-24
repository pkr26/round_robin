from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field

from .briefs import DraftBrief
from .common import Contract, Hash, Id


class CreateMeeting(Contract):
    command_id: UUID
    brief: DraftBrief


class SaveDraft(Contract):
    command_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    brief: DraftBrief


class SelectedParentItem(Contract):
    kind: Literal["evidence", "claim"]
    id: Id
    version: int = Field(ge=1, strict=True)
    sha256: Hash


class PreflightRequest(Contract):
    command_id: UUID
    expected_draft_revision: int = Field(ge=1, strict=True)
    mode: Literal["simulated", "live"] = "simulated"
    selected_parent_items: list[SelectedParentItem] = Field(default_factory=list, max_length=24)


class StartRun(Contract):
    command_id: UUID
    preflight_id: Id
    expected_draft_revision: int = Field(ge=1, strict=True)
    replace_parent_run_version: int | None = Field(default=None, ge=1, strict=True)


class RunCommand(Contract):
    command_id: UUID
    expected_run_version: int = Field(ge=1, strict=True)


class Clarification(RunCommand):
    brief: DraftBrief


class RetryStage(RunCommand):
    stage: Literal["synthesis", "audit"]


class FollowUp(Contract):
    command_id: UUID
    parent_run_id: Id
    expected_draft_revision: int = Field(ge=1, strict=True)
    brief: DraftBrief


class DeleteMeeting(Contract):
    command_id: UUID


class RecordOutcome(Contract):
    command_id: UUID
    run_id: Id
    report_version: int = Field(ge=1, strict=True)
    choice: Literal["accepted", "modified", "deferred", "rejected"]
    reason: str = Field(default="", max_length=2000)
    success_criterion: str = Field(default="", max_length=500)
    observed_outcome: str = Field(default="", max_length=4000)
    revisit_date: date | None = None
