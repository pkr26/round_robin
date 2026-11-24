"""Nonsecret, explicitly funded operator configuration; never inferred from keys."""
from datetime import date
from decimal import Decimal
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from .common import Contract, Money, Rate, ShortText
from .contributions import Provider
from .preflight import FinishPolicy, Limits


class Pricing(Contract):
    version: ShortText
    input_usd_per_million: Rate
    output_usd_per_million: Rate
    billable_output_bound_tokens: int = Field(ge=4096, le=200000, strict=True)
    reasoning_exposure_bounded: Literal[True]
    effective_date: date


class Capabilities(Contract):
    structured_text: Literal[True]
    context_window_tokens: int = Field(ge=8192, le=2000000, strict=True)
    verified_at: AwareDatetime
    evidence_version: ShortText


class AggregateFunding(Contract):
    deployment_daily_usd: Money
    deployment_monthly_usd: Money
    owner_daily_usd: Money
    owner_monthly_usd: Money


class ResearchConfiguration(Contract):
    enabled: bool = False
    pricing_version: ShortText | None = None
    per_request_usd: Money | None = None
    approved_public_region: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def funded_price(self) -> Self:
        if self.enabled and (not self.pricing_version or self.per_request_usd is None or Decimal(self.per_request_usd) <= 0):
            raise ValueError("Enabled public research requires versioned conservative request pricing")
        return self


class ProviderConfiguration(Contract):
    schema_version: Literal["1.0"] = "1.0"
    version: ShortText
    funded: Literal[True]
    protocol: Literal["independent_review_provisional"] = "independent_review_provisional"
    prompt_version: ShortText
    models: dict[Provider, str]
    pricing: dict[Provider, Pricing]
    capabilities: dict[Provider, Capabilities]
    limits: Limits
    finish_policy: FinishPolicy
    aggregate_funding: AggregateFunding
    anthropic_api_version: date
    expires_at: AwareDatetime
    fallbacks: dict[Literal["intake", "synthesis", "audit"], list[Provider]]
    research: ResearchConfiguration = Field(default_factory=ResearchConfiguration)

    @model_validator(mode="after")
    def complete_three_provider_scope(self) -> Self:
        expected = {"openai", "anthropic", "google"}
        if any(set(values) != expected for values in (self.models, self.pricing, self.capabilities)):
            raise ValueError("All three configured providers require pinned models, conservative pricing and verified capabilities")
        import re
        for provider, model in self.models.items():
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model) or provider == "google" and ("/" in model or ":" in model):
                raise ValueError("Provider model identifier is invalid")
        if set(self.fallbacks) != {"intake", "synthesis", "audit"} or any(not values or len(set(values)) != len(values) for values in self.fallbacks.values()):
            raise ValueError("Declare bounded moderator fallback ordering")
        if Decimal(self.finish_policy.reserved_usd) > Decimal(self.limits.per_run_usd) or self.finish_policy.reserved_requests > self.limits.max_requests or self.finish_policy.reserved_active_seconds > self.limits.active_seconds:
            raise ValueError("Protected finishing resources must fit inside original run limits")
        if any(Decimal(value) <= 0 for value in self.aggregate_funding.model_dump().values()):
            raise ValueError("Explicit aggregate funding limits must be positive")
        return self
