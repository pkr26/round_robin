from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from .briefs import PublicTerm
from .common import Contract, Hash, Id, Money, ShortText
from .contributions import Provider


class InputItem(Contract):
    id: Id
    version: int = Field(ge=1, strict=True)
    content_hash: Hash
    recipients: list[Provider] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def all_recipients(self) -> Self:
        if set(self.recipients) != {"openai", "anthropic", "google"}:
            raise ValueError("Every selected core input must share all three providers")
        return self


class InputManifest(Contract):
    items: list[InputItem] = Field(default_factory=list, max_length=5)
    content_hash: Hash


class Limits(Contract):
    max_requests: int = Field(default=24, ge=1, le=24, strict=True)
    max_calls_per_step: int = Field(default=2, ge=1, le=2, strict=True)
    active_seconds: int = Field(default=600, ge=1, le=600, strict=True)
    per_run_usd: Money = "2"
    owner_daily_usd: Money = "10"
    owner_monthly_usd: Money = "50"


class FinishPolicy(Contract):
    reserved_requests: int = Field(default=2, ge=2, le=2, strict=True)
    reserved_usd: Money = "0.40"
    reserved_active_seconds: int = Field(default=190, ge=1, le=600, strict=True)
    version: ShortText = "fictional-finish-v1"


class Preflight(Contract):
    id: Id
    fictional: Literal[True] = True
    draft_revision: int = Field(ge=1, strict=True)
    input_manifest: InputManifest
    draft_hash: Hash
    configuration_hash: Hash
    fingerprint: Hash
    recipients: list[Provider] = Field(min_length=3, max_length=3)
    approved_public_terms: list[PublicTerm] = Field(default_factory=list, max_length=8)
    query_templates: list[ShortText] = Field(default_factory=list, max_length=8)
    protocol: Literal["independent_review_provisional"] = "independent_review_provisional"
    models: dict[Provider, ShortText]
    prompt_version: ShortText = "fictional-prompts-v1"
    schema_version: Literal["1.0"] = "1.0"
    pricing_version: ShortText = "simulated-no-prices-v1"
    limits: Limits
    finish_policy: FinishPolicy
    created_at: AwareDatetime
    expires_at: AwareDatetime
    initial_admission_window_minutes: Literal[30] = 30
    consumed_by_run_id: Id | None = None
    paid_access_enabled: Literal[False] = False
    blockers: list[ShortText] = Field(default_factory=list, max_length=20)
    warnings: list[ShortText] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def exact_scope(self) -> Self:
        if set(self.recipients) != {"openai", "anthropic", "google"} or set(self.models) != set(self.recipients):
            raise ValueError("Core preflight requires the same three providers and model configuration")
        if (self.expires_at - self.created_at).total_seconds() != 600:
            raise ValueError("Preflight must expire exactly ten minutes after creation")
        if self.finish_policy.reserved_requests > self.limits.max_requests:
            raise ValueError("Finish calls exceed run limits")
        return self
