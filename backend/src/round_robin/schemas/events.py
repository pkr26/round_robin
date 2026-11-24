from typing import Literal

from pydantic import AwareDatetime, Field

from .common import Contract, Id, ShortText
from .reports import Readiness


class EventEnvelope(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: Id
    sequence: int = Field(ge=1, strict=True)
    timestamp: AwareDatetime
    event_type: Literal["run.admitted", "content.committed", "report.published", "run.cancelled"]
    record_id: Id | None = None
    description: ShortText


class RunSnapshot(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: Id
    revision: int = Field(ge=1, strict=True)
    event_cutoff: int = Field(ge=0, strict=True)
    status: Literal["queued", "running", "awaiting_input", "cancel_requested", "cancelled", "completed", "completed_with_limitations", "failed"]
    readiness: Readiness | None
    report_id: Id | None = None
    message_ids: list[Id] = Field(default_factory=list, max_length=40)
    next_message_cursor: str | None = Field(default=None, max_length=128)
