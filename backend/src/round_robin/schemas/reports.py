from typing import Literal, Self

from pydantic import Field, model_validator

from .common import Contract, Currency, Id, Money, ShortText, TotalMoney
from .contributions import ConstraintResult, Provider
from .evidence import Calculation

Readiness = Literal["ready_to_choose", "conditional", "no_feasible_option", "insufficient_evidence"]


class Recommendation(Contract):
    option_id: Id
    summary: ShortText
    conditions: list[ShortText] = Field(default_factory=list, max_length=20)


class OptionComparison(Contract):
    option_id: Id
    label: ShortText
    role: Literal["candidate", "status_quo"]
    total: TotalMoney | None
    known_subtotal: TotalMoney
    missing_components: list[ShortText] = Field(default_factory=list, max_length=35)
    currency: Currency
    horizon_months: int = Field(ge=1, le=120, strict=True)
    constraint_results: list[ConstraintResult] = Field(default_factory=list, max_length=20)
    calculation_id: Id
    caveat: ShortText

    @model_validator(mode="after")
    def unknown_total(self) -> Self:
        if self.missing_components and self.total is not None:
            raise ValueError("An incomplete cost cannot have an all-in total")
        return self


class EssentialDependency(Contract):
    id: Id
    description: ShortText
    status: Literal["supported", "assumed", "unverified", "mixed", "contradicted"]
    accepted_scoped_support: bool
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    origin: Literal["fictional", "user_supplied", "retrieved", "calculated"]


class NextEvidenceItem(Contract):
    id: Id
    question: ShortText
    affected_conclusion: ShortText
    verification_method: ShortText
    request_text: str = Field(min_length=1, max_length=2000)
    responsible_person: ShortText | None = None
    revisit_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ReviewCoverage(Contract):
    proposal_providers: list[Provider] = Field(default_factory=list, max_length=3)
    synthesizer: Provider | None = None
    auditor: Provider | None = None
    synthesis_valid: bool
    audit_succeeded: bool
    unresolved_critical_defect: bool = False


class ParticipantCoverage(Contract):
    provider: Provider
    role: ShortText
    status: Literal["simulated", "missing", "unavailable"]
    notes: ShortText


class Usage(Contract):
    status: Literal["fictional_no_spend"] = "fictional_no_spend"
    actual_cost: Money | None = "0"
    currency: Currency = "USD"
    request_count: Literal[0] = 0
    simulated: Literal[True] = True


class DecisionReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    id: Id
    run_id: Id
    parent_report_id: Id | None = None
    fictional: Literal[True] = True
    version: int = Field(ge=1, strict=True)
    event_cutoff: int = Field(ge=0, strict=True)
    execution_status: Literal["completed", "completed_with_limitations", "cancelled", "failed"]
    recommendation: Recommendation | None
    readiness: Readiness | None
    readiness_reasons: list[ShortText] = Field(min_length=1, max_length=20)
    readiness_unavailable_reason: ShortText | None = None
    main_reasons: list[ShortText] = Field(default_factory=list, max_length=10)
    tradeoff: ShortText
    largest_uncertainty: ShortText
    what_would_change_answer: ShortText
    next_action: ShortText
    comparison: list[OptionComparison] = Field(min_length=1, max_length=5)
    hard_requirement_ids: list[Id] = Field(max_length=20)
    essential_dependencies: list[EssentialDependency] = Field(default_factory=list, max_length=30)
    dissent: list[ShortText] = Field(default_factory=list, max_length=20)
    next_evidence: list[NextEvidenceItem] = Field(default_factory=list, max_length=20)
    calculations: list[Calculation] = Field(default_factory=list, max_length=10)
    coverage: list[ParticipantCoverage] = Field(max_length=6)
    assessment: ReviewCoverage
    usage: Usage = Field(default_factory=Usage)
    stop_reason: ShortText
    change_summary: list[ShortText] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def internally_consistent(self) -> Self:
        if not self.assessment.synthesis_valid and (self.recommendation is not None or self.readiness is not None):
            raise ValueError("Missing synthesis cannot publish a recommendation or readiness assessment")
        if self.readiness is None and self.recommendation is not None:
            raise ValueError("Unavailable assessment cannot publish an invented recommendation")
        if self.readiness is None and self.readiness_unavailable_reason is None:
            raise ValueError("Unavailable readiness needs an explicit reason")
        if self.readiness is not None and self.readiness_unavailable_reason is not None:
            raise ValueError("Available readiness cannot also be unavailable")
        if self.recommendation and self.recommendation.option_id not in {item.option_id for item in self.comparison}:
            raise ValueError("Recommendation must refer to a compared option")
        if self.readiness == "ready_to_choose":
            coverage = self.assessment
            if not (self.recommendation and coverage.synthesis_valid and coverage.audit_succeeded and coverage.auditor and coverage.synthesizer and coverage.auditor != coverage.synthesizer and not coverage.unresolved_critical_defect):
                raise ValueError("Ready publication needs valid synthesis and a successful distinct audit")
            if len(set(coverage.proposal_providers)) < 2:
                raise ValueError("Ready comparison requires two distinct validated proposals")
            selected = next(item for item in self.comparison if item.option_id == self.recommendation.option_id)
            if set(self.hard_requirement_ids) != {result.requirement_id for result in selected.constraint_results}:
                raise ValueError("Missing hard-requirement assessments cannot establish readiness")
            if any(item.result not in {"pass", "not_applicable"} for item in selected.constraint_results):
                raise ValueError("Selected option must pass all applicable hard requirements")
            if any(not item.basis_refs for item in selected.constraint_results):
                raise ValueError("Selected-option hard requirements need supporting references")
            if any(item.status != "supported" or not item.accepted_scoped_support or not item.reference_ids for item in self.essential_dependencies):
                raise ValueError("Essential dependencies need accepted scoped support")
        return self
