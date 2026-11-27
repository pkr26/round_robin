"""Authoritative saved-runtime report contract, separate from labeled samples."""

from datetime import date
from decimal import Decimal
import re
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, ValidationInfo, model_validator

from .common import Contract, Currency, Id, ShortText
from .evidence import Calculation
from .reports import EssentialDependency, OptionComparison, Readiness, ReviewCoverage
from .discussion import DecisionInsights, DiscussionIssue, ReadinessDimensions, ScopedAssessment


def runtime_cost(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,24})?", value) or Decimal(value) > Decimal("1e30"):
        raise ValueError("Runtime cost requires a bounded exact nonnegative decimal string")
    return value


RuntimeCost = Annotated[str, BeforeValidator(runtime_cost), Field(max_length=64, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,24})?$")]


class RuntimeUsage(Contract):
    simulated: bool
    status: Literal["reported", "unknown"]
    actual_cost: RuntimeCost | None
    conservative_exposure_usd: RuntimeCost
    unknown_operation_ids: list[Id] = Field(max_length=64)
    request_count: int = Field(ge=0, le=24, strict=True)
    currency: Literal["USD"] = "USD"

    @model_validator(mode="after")
    def uncertainty_is_preserved(self) -> Self:
        if self.status == "unknown" and (self.actual_cost is not None or not self.unknown_operation_ids):
            raise ValueError("Unknown usage needs retained operation identities and an unavailable final cost")
        if self.status == "reported" and (self.actual_cost is None or self.unknown_operation_ids):
            raise ValueError("Reported usage requires a final cost without unresolved operations")
        if len(set(self.unknown_operation_ids)) != len(self.unknown_operation_ids):
            raise ValueError("Unknown operations must be counted once")
        if self.simulated and self.actual_cost is not None and Decimal(self.actual_cost) != 0:
            raise ValueError("Simulation cannot report paid provider spending")
        return self


class RuntimeCoverage(Contract):
    provider: Literal["openai", "anthropic", "google"]
    role: ShortText
    status: Literal["pending", "claimed", "authorized", "succeeded", "failed", "skipped", "simulated", "missing", "unavailable"]
    notes: ShortText


class RuntimeRecommendation(Contract):
    option_id: Id
    option_label: ShortText | None = None
    summary: str = Field(min_length=1, max_length=4000)
    conditions: list[str] = Field(max_length=64)


class RuntimeNextEvidence(Contract):
    id: Id
    question: str = Field(min_length=1, max_length=2000)
    affected_conclusion: ShortText
    basis_refs: list[Id] = Field(default_factory=list, max_length=64, exclude_if=lambda refs: not refs)
    verification_method: ShortText
    request_text: str = Field(min_length=1, max_length=4000)
    responsible_person: ShortText | None = None
    revisit_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class RuntimeResearchSummary(Contract):
    status: Literal["not_requested", "unavailable", "pending", "running", "limited", "retrieved_unverified"]
    queries: int = Field(ge=0, le=6, strict=True)
    pages: int = Field(ge=0, le=8, strict=True)
    source_count: int | None = Field(default=None, ge=0, le=8, strict=True)
    reason: ShortText | None = None
    limitations: list[ShortText] = Field(default_factory=list, max_length=20)
    objectives: list[dict] = Field(default_factory=list, max_length=6)


class RuntimeOptionComparison(OptionComparison):
    known_subtotal: Annotated[str, BeforeValidator(runtime_cost)] | None


class RuntimeDecisionReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    id: Id
    run_id: Id
    parent_report_id: Id | None = None
    version: int = Field(ge=1, strict=True)
    valuation_date: date | None = None
    valuation_timezone: str | None = Field(default=None, min_length=1, max_length=64)
    evidence_valid_through: date | None = None
    event_cutoff: int = Field(ge=0, strict=True)
    fictional: bool
    simulated: bool
    execution_status: Literal["completed", "completed_with_limitations", "cancelled", "failed"]
    recommendation: RuntimeRecommendation | None
    readiness: Readiness | None
    readiness_reasons: list[str] = Field(min_length=1, max_length=64)
    readiness_unavailable_reason: str | None = Field(default=None, max_length=2000)
    main_reasons: list[str] = Field(max_length=20)
    tradeoff: str = Field(min_length=1, max_length=4000)
    tradeoff_basis_refs: list[Id] = Field(default_factory=list, max_length=64, exclude_if=lambda refs: not refs)
    largest_uncertainty: str = Field(min_length=1, max_length=2000)
    what_would_change_answer: str = Field(min_length=1, max_length=2000)
    next_action: str = Field(min_length=1, max_length=2000)
    comparison: list[RuntimeOptionComparison] = Field(max_length=5)
    # Five offers at the chosen horizon and at each side of a crossover
    # (before, at, after), plus the break-even calculation itself.
    calculations: list[Calculation] = Field(max_length=21)
    hard_requirement_ids: list[Id] = Field(max_length=20)
    evidence_ids: list[Id] = Field(max_length=24)
    contribution_ids: list[Id] = Field(max_length=24)
    claim_ids: list[Id] = Field(default_factory=list, max_length=200)
    essential_dependencies: list[EssentialDependency] = Field(max_length=30)
    dissent: list[str] = Field(max_length=20)
    next_evidence: list[RuntimeNextEvidence] = Field(max_length=20)
    next_evidence_count: int | None = Field(default=None, ge=0, strict=True, exclude_if=lambda count: count is None)
    coverage: list[RuntimeCoverage] = Field(max_length=24)
    assessment: ReviewCoverage
    usage: RuntimeUsage
    stop_reason: ShortText
    change_summary: list[str] = Field(max_length=20)
    research_summary: RuntimeResearchSummary
    algorithm_policy_version: str = Field(default="legacy_v1", max_length=64)
    issues: list[DiscussionIssue] = Field(default_factory=list, max_length=864)
    dissent_overflow_count: int = Field(default=0, ge=0, strict=True)
    evidence_assessments: list[ScopedAssessment] = Field(default_factory=list, max_length=300)
    readiness_dimensions: ReadinessDimensions | None = None
    decision_insights: DecisionInsights = Field(default_factory=DecisionInsights)

    @model_validator(mode="after")
    def publication_and_reference_gate(self, info: ValidationInfo) -> Self:
        if (self.valuation_date is None) != (self.valuation_timezone is None):
            raise ValueError("Valuation date and timezone must be recorded together")
        if self.valuation_timezone is not None:
            try:
                ZoneInfo(self.valuation_timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("Valuation requires a valid IANA timezone") from exc
        if self.evidence_valid_through is not None and (self.valuation_date is None or
                self.readiness == "ready_to_choose" and self.evidence_valid_through < self.valuation_date):
            raise ValueError("Ready decisions require evidence current on the saved valuation date")
        for values in (self.hard_requirement_ids, self.evidence_ids, self.contribution_ids, self.claim_ids):
            if len(set(values)) != len(values):
                raise ValueError("Authoritative scoped identities must be unique")
        if self.fictional != self.simulated or self.usage.simulated != self.simulated:
            raise ValueError("Runtime mode labels must agree across report and usage")
        option_ids = {item.option_id for item in self.comparison}
        calculation_ids = {item.id for item in self.calculations}
        if len(option_ids) != len(self.comparison) or len(calculation_ids) != len(self.calculations):
            raise ValueError("Compared options and calculations must have unique identities")
        references = set(self.evidence_ids) | calculation_ids | set(self.contribution_ids)
        if set(self.claim_ids) & references:
            raise ValueError("Claim identities cannot overlap source, calculation or contribution identities")
        references |= set(self.claim_ids)
        if len({item.id for item in self.issues}) != len(self.issues) or len({item.id for item in self.evidence_assessments}) != len(self.evidence_assessments):
            raise ValueError("Discussion and assessment identities must be unique")
        for issue in self.issues:
            if issue.source_step_id not in self.contribution_ids or not set(issue.reference_ids) <= references:
                raise ValueError("Discussion issue is outside the report contribution/reference scope")
            if issue.target_reference_id is not None and issue.target_reference_id not in references:
                raise ValueError("Discussion target is outside the report scope")
            if issue.option_id is not None and issue.option_id not in option_ids or issue.requirement_id is not None and issue.requirement_id not in self.hard_requirement_ids:
                raise ValueError("Discussion option or requirement is outside the report scope")
            if any(event.step_id not in self.contribution_ids or not set(event.reference_ids) <= references for event in issue.events):
                raise ValueError("Discussion resolution provenance is outside the report scope")
        requirement_receipts = {}
        for assessment in self.evidence_assessments:
            if assessment.run_id != self.run_id or assessment.option_id not in option_ids or not set(assessment.reference_ids) <= set(self.evidence_ids) or set(assessment.reference_ids) != set(assessment.evidence_hashes):
                raise ValueError("Scoped assessment does not match the report run/evidence scope")
            if len(assessment.reference_ids) != len(assessment.evidence_hashes):
                raise ValueError("Scoped assessments require unique passage identities")
            if assessment.target_type == "requirement" and assessment.requirement_id not in self.hard_requirement_ids or assessment.target_type == "option_costs" and assessment.requirement_id is not None:
                raise ValueError("Scoped assessment target is not valid for this report")
            if assessment.target_type == "requirement":
                target = (assessment.option_id, assessment.requirement_id)
                if target in requirement_receipts:
                    raise ValueError("A requirement can publish only one current assessment")
                requirement_receipts[target] = assessment
        if set(self.evidence_ids) & calculation_ids or set(self.evidence_ids) & set(self.contribution_ids) or calculation_ids & set(self.contribution_ids):
            raise ValueError("Source, calculation and contribution identities cannot overlap")
        context = info.context or {}
        if "allowed_evidence_ids" in context and set(self.evidence_ids) != set(context["allowed_evidence_ids"]):
            raise ValueError("Evidence scope must match the saved run")
        if "allowed_contribution_ids" in context and set(self.contribution_ids) != set(context["allowed_contribution_ids"]):
            raise ValueError("Contribution scope must match the saved run")
        if "allowed_claim_ids" in context and set(self.claim_ids) != set(context["allowed_claim_ids"]):
            raise ValueError("Claim scope must match the saved run")
        if context.get("run_id", self.run_id) != self.run_id:
            raise ValueError("Report belongs to another run")
        if "expected_simulated" in context and context["expected_simulated"] != self.simulated:
            raise ValueError("Report mode differs from the frozen run")
        if "hard_requirement_ids" in context and set(context["hard_requirement_ids"]) != set(self.hard_requirement_ids):
            raise ValueError("Report requirements differ from the frozen brief")
        if self.comparison and "allowed_option_ids" in context and set(context["allowed_option_ids"]) != option_ids:
            raise ValueError("Report must preserve every frozen considered option")
        if "parent_report_id" in context and context["parent_report_id"] != self.parent_report_id:
            raise ValueError("Report lineage does not match its saved parent")
        for calculation in self.calculations:
            if any(item.source_ref not in self.evidence_ids for item in calculation.inputs):
                raise ValueError("Calculation input source is outside this run's saved evidence")
        for option in self.comparison:
            if option.calculation_id not in calculation_ids:
                raise ValueError("Comparison needs its exact saved calculation")
            calculation = next(item for item in self.calculations if item.id == option.calculation_id)
            if (option.total is None) != (calculation.output is None) or option.total is not None and Decimal(option.total) != Decimal(calculation.output):
                raise ValueError("Comparison total must match the saved calculation")
            if calculation.currency != option.currency or calculation.horizon_months != option.horizon_months:
                raise ValueError("Comparison currency and horizon must match its calculation")
            if (option.known_subtotal is None) != (calculation.known_subtotal is None) or option.known_subtotal is not None and Decimal(option.known_subtotal) != Decimal(calculation.known_subtotal):
                raise ValueError("Comparison known subtotal must match its saved calculation")
            if option.missing_components != calculation.missing_components:
                raise ValueError("Comparison cost gaps must match its saved calculation")
            if len({result.requirement_id for result in option.constraint_results}) != len(option.constraint_results):
                raise ValueError("An option cannot assess the same hard requirement twice")
            for result in option.constraint_results:
                if result.requirement_id not in self.hard_requirement_ids or not (set(result.basis_refs) | set(result.counter_basis_refs)) <= references:
                    raise ValueError("Constraint result refers outside this report's frozen scope")
                if result.method == "human" and self.algorithm_policy_version != "legacy_v1":
                    receipt = requirement_receipts.get((option.option_id, result.requirement_id))
                    if receipt is None or result.basis_refs != receipt.reference_ids or result.result != ("unknown" if result.disputed else receipt.result):
                        raise ValueError("A human requirement verdict must retain its owner's exact selected passages and result")
        if any(not set(item.reference_ids) <= references for item in self.essential_dependencies):
            raise ValueError("Essential dependencies must resolve within this report")
        if self.recommendation and self.recommendation.option_id not in option_ids:
            raise ValueError("Recommendation refers to an unconsidered option")
        if not self.assessment.synthesis_valid and (self.recommendation is not None or self.readiness is not None):
            raise ValueError("Missing synthesis cannot invent a recommendation or readiness")
        if (self.readiness is None) != (self.readiness_unavailable_reason is not None):
            raise ValueError("Unavailable readiness requires an explicit reason")
        if not self.comparison:
            if self.execution_status == "completed" or self.recommendation is not None or self.readiness is not None:
                raise ValueError("An empty comparison can publish only an explicit unavailable partial assessment")
        elif self.readiness is not None:
            from round_robin.reports.readiness import derive_readiness
            derived, _ = derive_readiness(selected_option_id=self.recommendation.option_id if self.recommendation else None, comparison=self.comparison, dependencies=self.essential_dependencies, coverage=self.assessment, required_requirement_ids=set(self.hard_requirement_ids))
            if self.readiness != derived:
                raise ValueError("Published readiness does not match the authoritative reducer")
        if self.readiness == "ready_to_choose" and (self.recommendation is None or self.simulated):
            raise ValueError("A simulated runtime cannot certify an actual ready choice")
        if self.readiness == "ready_to_choose":
            selected = next(item for item in self.comparison if item.option_id == self.recommendation.option_id)
            if selected.total is None or selected.missing_components:
                raise ValueError("An incomplete comparable cost cannot establish a ready runtime choice")
            if any(issue.severity in {"critical", "material"} and issue.status in {"open", "answered_pending_review"} for issue in self.issues):
                raise ValueError("Unresolved material discussion obligations cannot establish readiness")
            if self.readiness_dimensions and not all((self.readiness_dimensions.complete_comparison, self.readiness_dimensions.checked_arithmetic,
                self.readiness_dimensions.scoped_cost_support, self.readiness_dimensions.requirements_assessed,
                self.readiness_dimensions.distinct_review, self.readiness_dimensions.material_objections_resolved)):
                raise ValueError("Published readiness dimensions do not establish a ready decision")
        if self.recommendation and self.recommendation.option_label is not None:
            selected = next(option for option in self.comparison if option.option_id == self.recommendation.option_id)
            if selected.label != self.recommendation.option_label:
                raise ValueError("Recommendation label must match the selected option")
        insights = self.decision_insights
        if insights.tied_lowest_cost_option_ids or insights.failed_option_ids or insights.cost_comparison_blocker or self.algorithm_policy_version == "comparable_cost_choices_v2":
            from round_robin.reports.insights import cost_comparison
            ranking = cost_comparison(set(self.hard_requirement_ids), [item.model_dump(mode="json") for item in self.comparison])
            if any(getattr(insights, key) != value for key, value in ranking.items()):
                raise ValueError("Reported cost comparison must match its options, hard requirements and supporting evidence")
            if self.simulated and self.recommendation and (insights.cost_comparison_blocker or insights.cost_frontier_option_ids != [self.recommendation.option_id]):
                raise ValueError("A simulated choice requires a unique, comparable lowest-cost option")
        if self.next_evidence_count is not None and self.next_evidence_count < len(self.next_evidence):
            raise ValueError("Outstanding question count must cover every saved question")
        for basis in [self.tradeoff_basis_refs, *(item.basis_refs for item in self.next_evidence)]:
            if len(set(basis)) != len(basis) or not set(basis) <= references:
                raise ValueError("Tradeoff and question citations must resolve uniquely within this report")
        return self


RuntimeReport = RuntimeDecisionReport
