"""Deterministic arbitrary-brief behavior; it cannot establish model quality."""

from datetime import date
from decimal import Decimal
import time

from round_robin.reports.insights import comparable_cost_leaders
from round_robin.schemas.briefs import DraftBrief

from .base import ProviderFailure, ProviderOutput, ProviderRequest, ProviderResult, validate_output


class SimulatedProvider:
    def __init__(self, provider: str, scenario: str = "success"):
        self.provider = provider
        self.model = f"simulated-{provider}-v1"
        self.scenario = scenario
        self.calls = 0

    def conservative_bound(self, request: ProviderRequest) -> Decimal:
        request.packet()
        return Decimal("0")

    def generate(self, request: ProviderRequest) -> ProviderResult:
        request.packet()
        self.calls += 1
        if self.scenario == "test_slow_success":
            # Explicit frozen synthetic fault profile for disposable process
            # tests; never enabled from a brief or in a live adapter.
            time.sleep(min(60, request.timeout_seconds))
            self.scenario = "success"
        if self.scenario != "success":
            raise ProviderFailure(self.scenario, retryable=self.scenario in {"timeout", "rate_limit", "transient", "malformed"}, may_have_sent=self.scenario != "before_send", cost_usd=Decimal("0"))
        brief = DraftBrief.model_validate(request.brief)
        questions = []
        if request.stage == "intake":
            if len([o for o in brief.options if o.role == "candidate"]) < 2:
                questions.append("Add two to four confirmed project options, or explicitly describe the alternatives to compare.")
            if brief.horizon_months is None:
                questions.append("Confirm a common comparison horizon from 1 to 120 months.")
            if not brief.hard_requirements:
                questions.append("Confirm the hard requirements, or explicitly accept a cost-only comparison with other feasibility unknown.")
        leaders = []
        if brief.horizon_months:
            from round_robin.orchestration.reporting import comparison_data
            comparisons, _ = comparison_data(brief, as_of=date.fromisoformat(request.cost_as_of) if request.cost_as_of else None)
            leaders = comparable_cost_leaders({item.id for item in brief.hard_requirements}, comparisons)
        selected = leaders[0] if len(leaders) == 1 else None
        if selected:
            summary = "One option has the lowest comparable cost under the assessed requirements."
        elif leaders:
            summary = "Multiple options share the lowest comparable cost. The supplied costs establish no single preferred option."
        else:
            summary = "The supplied costs and hard-requirement results do not establish a preferred option."
        # The independent simulated audit checks references/format, not truth.
        output = ProviderOutput(
            summary=f"Simulated {request.stage} for ‘{brief.title}’. {summary}",
            recommendation_option_id=selected if request.stage != "intake" else None,
            reference_ids=sorted(request.allowed_reference_ids)[:40], questions=questions[:3],
            assumptions=["Deterministic simulation; no provider independently reviewed the facts or recommendation."],
            constraints=[], audit_passed=True if request.stage == "audit" else None,
            critical_defects=[], dissent=["Lowest known cost alone does not establish practical suitability."] if selected else [],
        )
        validate_output(output, request)
        return ProviderResult(output, Decimal("0"), {"input_tokens": 0, "output_tokens": 0}, True, self.provider, self.model)
