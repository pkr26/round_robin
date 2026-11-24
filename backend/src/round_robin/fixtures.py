"""One coherent fictional comparison and deterministic changed-input examples."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from .research.calculator import break_even, lifecycle_total
from .reports.readiness import derive_readiness
from .schemas import Contribution, DecisionReport, DraftBrief, EvidencePassage, Preflight
from .schemas.briefs import Constraint, CostComponent, Option, Preference
from .schemas.common import Contract, Id, ShortText
from .schemas.contributions import Claim, ConstraintResult
from .schemas.preflight import FinishPolicy, InputManifest, Limits
from .schemas.reports import EssentialDependency, NextEvidenceItem, OptionComparison, ParticipantCoverage, Recommendation, ReviewCoverage

FIXTURE_TIME = datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class ActivityItem(Contract):
    id: Id
    stage: ShortText
    title: ShortText
    description: str = Field(min_length=1, max_length=2000)
    status: Literal["simulated", "saved", "limited"]
    provider: Literal["openai", "anthropic", "google"] | None = None
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)


class SampleScenario(Contract):
    id: Literal["base12", "horizon24", "missing_installation"]
    label: ShortText
    description: ShortText
    brief: DraftBrief
    report: DecisionReport | None
    preflight: Preflight
    partial_report: DecisionReport
    cancelled_report: DecisionReport


class SampleBundle(Contract):
    schema_version: Literal["1.0"] = "1.0"
    fictional: Literal[True] = True
    brief: DraftBrief
    evidence: list[EvidencePassage] = Field(max_length=24)
    contributions: list[Contribution] = Field(max_length=20)
    scenarios: list[SampleScenario] = Field(min_length=3, max_length=3)
    partial_report: DecisionReport
    cancelled_report: DecisionReport
    activity: list[ActivityItem] = Field(max_length=40)

    @model_validator(mode="after")
    def scoped_fixture_references(self) -> Self:
        evidence_ids = {passage.id for passage in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique")
        if {scenario.id for scenario in self.scenarios} != {"base12", "horizon24", "missing_installation"}:
            raise ValueError("The sample requires its three distinct changed-input scenarios")
        for scenario in self.scenarios:
            if fingerprint(scenario.brief.model_dump(mode="json")) != scenario.preflight.draft_hash:
                raise ValueError("Preflight must bind the exact scenario brief")
            if scenario.report:
                known = evidence_ids | {calculation.id for calculation in scenario.report.calculations}
                for dependency in scenario.report.essential_dependencies:
                    if not set(dependency.reference_ids) <= known:
                        raise ValueError("Dependency references must resolve within the fixture")
                for option in scenario.brief.options:
                    if not {component.source_ref for component in option.cost_components} <= evidence_ids:
                        raise ValueError("Cost provenance must resolve to exact stored evidence")
                requirements = {requirement.id for requirement in scenario.brief.hard_requirements}
                for option in scenario.report.comparison:
                    if {result.requirement_id for result in option.constraint_results} != requirements:
                        raise ValueError("Every compared option must assess every hard requirement")
                    if not all(set(result.basis_refs) <= known for result in option.constraint_results):
                        raise ValueError("Constraint basis references must resolve in the same scenario")
        base_report = next(scenario.report for scenario in self.scenarios if scenario.id == "base12")
        if base_report is None:
            raise ValueError("Base fixture report is required to resolve contribution references")
        known = evidence_ids | {calculation.id for calculation in base_report.calculations}
        for contribution in self.contributions:
            refs = set(contribution.reference_ids)
            for claim in contribution.claims:
                refs.update(claim.reference_ids)
            if not refs <= known:
                raise ValueError("Contribution references must resolve within the supplied sample scope")
        return self


def sample_brief(horizon: int = 12, missing_installation: bool = False) -> DraftBrief:
    def component(id: str, label: str, kind: str, amount: str | None, source: str, frequency: str = "once") -> CostComponent:
        return CostComponent(id=id, label=label, kind=kind, amount=amount, currency="USD", frequency=frequency, source_ref=source, unknown_reason="Installation is omitted from the supplied quote." if amount is None else None)
    return DraftBrief(revision=1 if horizon == 12 else 2 if not missing_installation else 3, title="Choose a workshop label system", question="Which label system best fits our small workshop's budget and planning horizon?", goal="Replace handwritten labels with a repeatable label system for workshop orders.", background="Entirely fictional sample. These values are arithmetic fixtures, not supplier quotes or market estimates.", currency="USD", budget="2500", horizon_months=horizon, deadline="2026-12-15", timezone="America/Phoenix", location="Fictional workshop; no address provided", options=[
        Option(id="option_a", label="Option A · lower upfront", provenance="fictional", cost_components=[component("cost_a_upfront", "Upfront purchase", "upfront", "1000", "passage_offer_a"), component("cost_a_monthly", "Monthly supplies and service", "recurring", "50", "passage_offer_a", "monthly"), component("cost_a_installation", "Installation (explicitly included)", "installation", "0", "passage_offer_a")], consequences=["Lower initial commitment; monthly costs accumulate faster."]),
        Option(id="option_b", label="Option B · lower recurring", provenance="fictional", cost_components=[component("cost_b_upfront", "Upfront purchase", "upfront", "1600", "passage_installation_gap" if missing_installation else "passage_offer_b"), component("cost_b_monthly", "Monthly supplies and service", "recurring", "20", "passage_installation_gap" if missing_installation else "passage_offer_b", "monthly"), component("cost_b_installation", "Required installation", "installation", None if missing_installation else "0", "passage_installation_gap" if missing_installation else "passage_offer_b")], consequences=["Higher initial commitment; lower recurring cost."]),
        Option(id="option_status_quo", label="Keep handwritten labels", role="status_quo", provenance="fictional", cost_components=[component("cost_status_quo", "New spending (explicit zero)", "upfront", "0", "passage_goal")], consequences=["Reversible and avoids a purchase; does not meet the stated replacement goal."])
    ], hard_requirements=[Constraint(id="requirement_budget", description="All-in cost must remain within USD 2,500 over the chosen horizon.", origin="fictional", metric="total_cost", operator="lte", threshold="2500"), Constraint(id="requirement_goal", description="Replace handwritten labels with a repeatable label system.", origin="fictional", metric="manual", operator="manual")], preferences=[Preference(id="preference_cost", description="Prefer the lower comparable cost after hard requirements are met.", rank=1, origin="fictional"), Preference(id="preference_reversible", description="Prefer a smaller initial commitment when costs are otherwise comparable.", rank=2, origin="fictional")], user_facts=["Fictional scenario: both purchased systems meet the stated workflow need.", "Tax, delivery and maintenance are explicitly included at zero additional cost; B installation remains unknown in the changed quote." if missing_installation else "Tax, delivery, maintenance and installation are explicitly included at zero additional cost in the complete fictional quotes.", "Keeping the current workflow is feasible but does not achieve the replacement goal."], selected_attachment_ids=[], public_research_terms=[])


def sample_evidence() -> list[EvidencePassage]:
    records = [
        ("passage_offer_a", "source_offer_a", "Fictional offer A", "Offer A, cost lines 1–3", "Option A costs USD 1,000 upfront and USD 50 per month. Installation, delivery, tax and maintenance are included at zero additional cost. It meets the specified replacement workflow.", "offer_a"),
        ("passage_offer_b", "source_offer_b", "Fictional offer B", "Offer B, cost lines 1–3", "Option B costs USD 1,600 upfront and USD 20 per month. Installation, delivery, tax and maintenance are included at zero additional cost. It meets the specified replacement workflow.", "offer_b"),
        ("passage_goal", "source_owner_brief", "Fictional owner brief", "Confirmed goal and preferences, paragraphs 1–2", "Replace handwritten labels with a repeatable label system. The all-in budget is USD 2,500 over the selected horizon. Prefer the lower comparable cost. The existing handwritten approach requires zero new spending but does not meet this replacement goal.", "owner_brief"),
        ("passage_installation_gap", "source_offer_b_changed", "Fictional changed offer B", "Changed-input fixture, omitted installation line", "Changed-input example only: Option B's required installation amount is omitted. USD 1,600 upfront and USD 20 per month are known, but the all-in cost remains unknown until installation is quoted. It continues to meet the specified replacement workflow.", "offer_b"),
    ]
    return [EvidencePassage(id=id, owner_id="owner_local_fixture", run_id="run_fictional_sample", source_id=source, source_title=title, locator=locator, text=text, sha256=hashlib.sha256(text.encode()).hexdigest(), verified_at=FIXTURE_TIME, effective_date=FIXTURE_TIME.date(), origin="fictional", verification_status="fictional", scope="Arithmetic and interface demonstration only; no real supplier or independent verification.", time_sensitive=True, origin_group=f"group_{group}") for id, source, title, locator, text, group in records]


def sample_preflight(brief: DraftBrief) -> Preflight:
    limits = Limits()
    configuration = {"recipients": ["openai", "anthropic", "google"], "protocol": "independent_review_provisional", "limits": limits.model_dump(mode="json"), "mode": "offline_only"}
    return Preflight(id=f"preflight_revision_{brief.revision}", draft_revision=brief.revision, input_manifest=InputManifest(items=[], content_hash=fingerprint([])), draft_hash=fingerprint(brief.model_dump(mode="json")), configuration_hash=fingerprint(configuration), fingerprint=fingerprint({"draft": brief.model_dump(mode="json"), "configuration": configuration}), recipients=["openai", "anthropic", "google"], approved_public_terms=brief.public_research_terms, query_templates=[], models={"openai": "fictional-openai-fixture", "anthropic": "fictional-claude-fixture", "google": "fictional-gemini-fixture"}, limits=limits, finish_policy=FinishPolicy(), created_at=FIXTURE_TIME, expires_at=FIXTURE_TIME + timedelta(minutes=10), blockers=["Paid access is disabled in the Phase 0 prototype."], warnings=["Static fictional preflight. No Start command, provider request, public search or monetary authorization exists.", "The protocol and USD 2 ceiling are provisional and unvalidated."])


def sample_report(brief: DraftBrief, *, scenario_id: str, partial: bool = False) -> DecisionReport:
    horizon = brief.horizon_months
    assert horizon is not None
    comparisons = []
    calculations = []
    for option in brief.options:
        calculation = lifecycle_total(option, horizon, brief.currency, calculation_id=f"calc_{option.id}_{horizon}")
        calculations.append(calculation)
        budget_result = "unknown" if calculation.output is None else "pass" if Decimal(calculation.output) <= Decimal(brief.budget or "0") else "fail"
        comparisons.append(OptionComparison(option_id=option.id, label=option.label, role=option.role, total=calculation.output, known_subtotal=calculation.known_subtotal, missing_components=calculation.missing_components, currency=brief.currency, horizon_months=horizon, calculation_id=calculation.id, constraint_results=[ConstraintResult(requirement_id="requirement_budget", result=budget_result, basis_refs=[calculation.id], method="deterministic_calculation", reason="All-in cost is unknown because a required component is missing." if budget_result == "unknown" else f"Calculated all-in cost is {calculation.output} USD against a {brief.budget} USD limit."), ConstraintResult(requirement_id="requirement_goal", result="fail" if option.role == "status_quo" else "pass", basis_refs=["passage_goal"], method="fixture_assessment", reason="Keeping handwritten labels does not meet the stated replacement goal." if option.role == "status_quo" else "The fictional task explicitly states this system meets the workflow need.")], caveat="Explicit zero new spending; leaves the replacement goal unmet." if option.role == "status_quo" else "Fictional complete cost inputs; no real quote verification." if calculation.output is not None else "Known subtotal only; installation is unknown, not zero."))
    missing = any(item.missing_components for item in calculations)
    if not missing:
        calculations.append(break_even(brief.options[0], brief.options[1], brief.currency))
    chosen = "option_a" if horizon < 20 or missing else "option_b"
    dependencies = [EssentialDependency(id="dependency_comparable_costs", description="The supplied costs include every required component over a common horizon.", status="unverified" if missing else "supported", accepted_scoped_support=not missing, reference_ids=["passage_installation_gap"] if missing else ["passage_offer_a", "passage_offer_b"], origin="fictional"), EssentialDependency(id="dependency_goal", description="Both purchase options meet the owner-confirmed replacement goal.", status="supported", accepted_scoped_support=True, reference_ids=["passage_goal", "passage_offer_a", "passage_installation_gap" if missing else "passage_offer_b"], origin="fictional")]
    assessment = ReviewCoverage(proposal_providers=["openai", "anthropic", "google"], synthesizer=None if partial else "openai", auditor=None if partial else "google", synthesis_valid=not partial, audit_succeeded=not partial)
    readiness, reasons = derive_readiness(selected_option_id=None if partial else chosen, comparison=comparisons, dependencies=dependencies, coverage=assessment, required_requirement_ids={requirement.id for requirement in brief.hard_requirements})
    condition = "Confirm Option B's missing installation cost before claiming the cheapest all-in choice." if missing else "Within this fictional brief and its explicit complete cost assumptions."
    recommendation = None if partial else Recommendation(option_id=chosen, summary="Option A is a provisional fallback; a cheapest all-in choice is not established." if missing else "Choose Option A for the 12-month horizon." if horizon == 12 else "Choose Option B for the 24-month horizon.", conditions=[condition])
    return DecisionReport(id=f"report_{scenario_id}" + ("_partial" if partial else ""), run_id="run_fictional_sample", parent_report_id="report_base12" if scenario_id != "base12" else None, version=1, event_cutoff=5 if partial else 9, execution_status="completed_with_limitations" if partial else "completed", recommendation=recommendation, readiness=readiness, readiness_reasons=reasons, readiness_unavailable_reason=reasons[0] if readiness is None else None, main_reasons=[] if partial else ["At 12 months, A totals USD 1,600 and B totals USD 1,840." if horizon == 12 else "At 24 months, A totals USD 2,200; B has USD 2,080 in known costs." if missing else "At 24 months, B totals USD 2,080 and A totals USD 2,200.", "The status quo is considered but fails the stated replacement goal.", "A/B costs are equal at 20 months when both quotes are complete." if not missing else "Unknown installation prevents a complete all-in comparison."], tradeoff="A uses USD 600 less upfront but costs USD 30 more each month.", largest_uncertainty="Option B's required installation amount is missing." if missing else "These fictional quote assumptions must be replaced with current, complete real quotes.", what_would_change_answer="A planning horizon longer than 20 months favors B on complete costs; shorter than 20 months favors A. At 20 months they tie.", next_action="Request Option B's installation quote and confirm its all-in cost." if missing else "Obtain written, all-inclusive quotes and confirm the planning horizon before choosing.", comparison=comparisons, hard_requirement_ids=[requirement.id for requirement in brief.hard_requirements], essential_dependencies=dependencies, dissent=["A lower initial payment and a lower long-term total favor different options.", "Keeping the current system remains available if replacement is deferred."], next_evidence=[NextEvidenceItem(id="next_quote", question="What is the complete installation cost for B?" if missing else "Do the real written quotes include all required charges?", affected_conclusion="Whether B has the lower all-in cost at 24 months." if missing else "The option cost comparison and budget pass results.", verification_method="Request a dated itemized written quote and confirm all exclusions.", request_text=f"Please quote a label system over {horizon} months, listing upfront, installation, recurring supplies, maintenance, delivery and tax. Explicitly state any zero, exclusion, validity date and workflow limitation. The planning budget is USD 2,500.")], calculations=calculations, coverage=[ParticipantCoverage(provider="openai", role="Solution design; synthesis" if not partial else "Solution design", status="simulated", notes="Manually authored fixture, not a provider response."), ParticipantCoverage(provider="anthropic", role="Cost and delivery", status="simulated", notes="Manually authored fixture, not a provider response."), ParticipantCoverage(provider="google", role="Risk review; final audit", status="missing" if partial else "simulated", notes="Audit and synthesis are unavailable in this partial fixture." if partial else "Manually authored distinct-auditor fixture; no model quality claim.")], assessment=assessment, stop_reason="synthesis_unavailable" if partial else "fictional_fixture_complete", change_summary=[] if scenario_id == "base12" else [f"Planning horizon changed from 12 to {horizon} months.", "Earlier report remains unchanged.", "B's installation input changed from explicit zero to unknown; no cheapest all-in claim is published." if missing else "Recommendation changes from A to B because the cost ordering reverses after month 20."])


def sample_bundle() -> SampleBundle:
    base = sample_brief()
    changed = sample_brief(24)
    missing = sample_brief(24, missing_installation=True)
    partial = sample_report(base, scenario_id="base12", partial=True)
    cancelled = DecisionReport.model_validate({**partial.model_dump(mode="json"), "id": "report_cancelled_sample", "execution_status": "cancelled", "stop_reason": "user_cancellation_fixture", "readiness_reasons": ["The fictional run was stopped before synthesis; no recommendation or readiness assessment exists."], "readiness_unavailable_reason": "Cancelled before synthesis; committed comparison work is preserved."})
    def scenario_fixture(brief: DraftBrief, id: str, label: str, description: str) -> SampleScenario:
        partial_report = sample_report(brief, scenario_id=id, partial=True)
        cancelled_report = DecisionReport.model_validate({**partial_report.model_dump(mode="json"), "id": f"report_{id}_cancelled", "execution_status": "cancelled", "stop_reason": "user_cancellation_fixture", "readiness_reasons": ["The fictional run was stopped before synthesis; no recommendation or readiness assessment exists."], "readiness_unavailable_reason": "Cancelled before synthesis; committed comparison work is preserved."})
        return SampleScenario(id=id, label=label, description=description, brief=brief, report=sample_report(brief, scenario_id=id), preflight=sample_preflight(brief), partial_report=partial_report, cancelled_report=cancelled_report)
    contributions = [
        Contribution(id="contribution_openai", provider="openai", model="fictional-openai-fixture", role="Solution design", stage="proposal", summary="A fits the 12-month plan with the smaller initial commitment.", public_rationale="Using only the brief and shared cost snapshot, A costs USD 1,600 over 12 months. It meets the replacement goal in this fictional case. Keep the current approach as a deliberate defer alternative.", claims=[Claim(alias="new_claim_1", proposition="A's 12-month total is USD 1,600.", kind="calculation", evidence_status="fictional", reference_ids=["calc_option_a_12"])], reference_ids=["passage_offer_a", "passage_goal"], assumptions=["All specified quote components are complete in the base fixture."]),
        Contribution(id="contribution_anthropic", provider="anthropic", model="fictional-claude-fixture", role="Cost and delivery", stage="proposal", summary="The planning horizon is decisive: B becomes cheaper after month 20.", public_rationale="Using the same shared inputs, B's USD 600 higher upfront cost is offset by USD 30 per month in savings. This makes the break-even horizon 20 months. At 12 months, B costs USD 1,840.", claims=[Claim(alias="new_claim_1", proposition="The complete A/B costs are equal at 20 months.", kind="calculation", evidence_status="fictional", reference_ids=["calc_break_even"])], reference_ids=["passage_offer_a", "passage_offer_b"]),
        Contribution(id="contribution_google", provider="google", model="fictional-gemini-fixture", role="Risks and missing evidence", stage="proposal", summary="Check all-in quotes; an omitted installation charge must remain unknown.", public_rationale="Both purchases meet the fictional workflow requirement. Their price ordering depends on the horizon and complete charges. Keeping handwritten labels avoids new spending but fails the replacement goal. Agreement among these manually authored fixtures does not verify real supplier facts.", reference_ids=["passage_goal", "passage_offer_b"], open_questions=["Will real quotes preserve the complete-cost assumptions?"])
    ]
    return SampleBundle(brief=base, evidence=sample_evidence(), contributions=contributions, scenarios=[scenario_fixture(base, "base12", "12-month comparison", "Complete fictional costs favor A over the 12-month planning horizon."), scenario_fixture(changed, "horizon24", "Follow-up: 24 months", "Changing only the planning horizon reverses the cost ordering after month 20."), scenario_fixture(missing, "missing_installation", "Missing installation quote", "B's 24-month known subtotal is USD 2,080; its all-in total and budget result are unknown.")], partial_report=partial, cancelled_report=cancelled, activity=[ActivityItem(id="activity_brief", stage="Intake", title="Fictional brief saved", description="An editable typed brief and common cost horizon were prepared without inference.", status="saved", reference_ids=["passage_goal"]), ActivityItem(id="activity_proposals", stage="Independent proposals", title="Three manually authored proposals", description="Each fixture uses the same brief and evidence. These are independent perspective examples, not actual model responses.", status="simulated", reference_ids=["contribution_openai", "contribution_anthropic", "contribution_google"]), ActivityItem(id="activity_calculations", stage="Cost check", title="Decimal totals checked", description="Exact component arithmetic yields A 1,600 and B 1,840 at 12 months; equality is 20 months.", status="saved", reference_ids=["calc_option_a_12", "calc_option_b_12", "calc_break_even"]), ActivityItem(id="activity_report", stage="Publication", title="Version 1 fixture report", description="Readiness is computed by application rules from fixture assessments and distinct audit coverage. No real review occurred.", status="simulated", reference_ids=["report_base12"]), ActivityItem(id="activity_follow_up", stage="Follow-up", title="Changed inputs preserve history", description="The 24-month and missing-installation examples are separate saved fictional reports.", status="saved", reference_ids=["report_horizon24", "report_missing_installation"])])
