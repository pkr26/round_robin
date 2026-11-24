"""Attributed discussion obligations and scoped human assessments, not truth scores."""
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from .application import RunCommand
from .common import Contract, Hash, Id, ShortText


class ObjectionEvent(Contract):
    id: Id
    step_id: Id
    provider: str = Field(max_length=96)
    stage: str = Field(max_length=32)
    disposition: Literal["opened", "answered", "resolved", "withdrawn", "reopened"]
    response: ShortText
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    input_hash: Hash


class DiscussionIssue(Contract):
    id: Id
    concern: ShortText
    severity: Literal["minor", "material", "critical"]
    target_reference_id: Id | None = None
    option_id: Id | None = None
    requirement_id: Id | None = None
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    status: Literal["open", "answered_pending_review", "resolved", "withdrawn"] = "open"
    origin: Literal["structured", "critical_defect", "dissent"]
    source_step_id: Id
    source_provider: str = Field(max_length=96)
    source_stage: str = Field(max_length=32)
    events: list[ObjectionEvent] = Field(default_factory=list, max_length=48)


class ScopedAssessmentCommand(RunCommand):
    target_type: Literal["option_costs", "requirement"]
    option_id: Id
    requirement_id: Id | None = None
    target_hash: Hash
    reference_ids: list[Id] = Field(min_length=1, max_length=24)
    result: Literal["supported", "pass", "fail", "unknown", "not_applicable"]
    attestation: str = Field(min_length=20, max_length=2000)
    valid_until: date | None = None


class ScopedAssessment(Contract):
    id: Id
    target_type: Literal["option_costs", "requirement"]
    option_id: Id
    requirement_id: Id | None = None
    target_hash: Hash
    reference_ids: list[Id] = Field(min_length=1, max_length=24)
    evidence_hashes: dict[Id, Hash] = Field(max_length=24)
    result: Literal["supported", "pass", "fail", "unknown", "not_applicable"]
    attestation: str = Field(min_length=20, max_length=2000)
    method: Literal["user_attestation"] = "user_attestation"
    scope: Literal["Exact supplied inputs attested by the owner; not independent supplier verification."] = "Exact supplied inputs attested by the owner; not independent supplier verification."
    actor_id: Id
    run_id: Id
    recorded_at: AwareDatetime
    valid_until: date | None = None
    supersedes_id: Id | None = None


class AssessmentTargetView(Contract):
    target_type: Literal["option_costs", "requirement"]
    option_id: Id
    requirement_id: Id | None = None
    target_hash: Hash
    label: str = Field(max_length=1200)
    reference_ids: list[Id] = Field(default_factory=list, max_length=24)


class AssessmentPage(Contract):
    items: list[ScopedAssessment] = Field(default_factory=list, max_length=300)
    current_items: list[ScopedAssessment] = Field(default_factory=list, max_length=105)
    targets: list[AssessmentTargetView] = Field(default_factory=list, max_length=105)
    run_version: int = Field(ge=1)
    scope_notice: str = Field(max_length=1000)
    report_version: int | None = Field(default=None, ge=0)


class ReadinessDimensions(Contract):
    complete_comparison: bool
    checked_arithmetic: bool
    scoped_cost_support: bool
    requirements_assessed: bool
    distinct_review: bool
    material_objections_resolved: bool
    basis_scope: str = Field(max_length=500)


class CostSensitivity(Contract):
    option_id: Id
    requirement_id: Id
    threshold: str
    total: str
    margin: str
    unit: str
    scope: str = "Arithmetic using supplied costs; not a probability or forecast."


class DecisionInsights(Contract):
    cost_frontier_option_ids: list[Id] = Field(default_factory=list, max_length=5)
    dominated_option_ids: list[Id] = Field(default_factory=list, max_length=5)
    unresolved_option_ids: list[Id] = Field(default_factory=list, max_length=5)
    sensitivity: list[CostSensitivity] = Field(default_factory=list, max_length=100)
    preference_order: list[Id] = Field(default_factory=list, max_length=10)
    limitations: list[ShortText] = Field(default_factory=list, max_length=10)
