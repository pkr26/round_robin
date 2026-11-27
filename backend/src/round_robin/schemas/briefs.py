from datetime import date
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from .common import Contract, Currency, Id, Money, Quantity, ShortText


def deadline_requirement_description(deadline: date | str) -> str:
    """The explicit dated requirement preserved in resolved briefs."""
    return f"Complete by {deadline}; obtain a committed applicable supplier or project schedule."


class CostComponent(Contract):
    id: Id
    label: ShortText
    kind: Literal["upfront", "installation", "recurring", "maintenance", "tax_delivery", "discount", "other"]
    amount: Money | None
    currency: Currency
    quantity: Quantity = "1"
    frequency: Literal["once", "monthly"]
    status: Literal["included", "excluded", "not_applicable"] = "included"
    unknown_reason: ShortText | None = None
    source_ref: Id
    unit: Literal["item", "month"] = "item"
    quote_valid_until: date | None = None

    @model_validator(mode="after")
    def explicit_unknown(self) -> Self:
        if self.status == "included" and self.amount is None and self.unknown_reason is None:
            raise ValueError("An unknown included component requires an explicit reason")
        if self.amount is not None and self.unknown_reason is not None:
            raise ValueError("A known amount cannot also be unknown")
        return self


class Option(Contract):
    id: Id
    label: ShortText
    role: Literal["candidate", "status_quo"] = "candidate"
    provenance: Literal["user_supplied", "assumed", "fictional"]
    cost_components: list[CostComponent] = Field(default_factory=list, max_length=30)
    consequences: list[ShortText] = Field(default_factory=list, max_length=10)


class Constraint(Contract):
    id: Id
    description: ShortText
    origin: Literal["user_supplied", "assumed", "fictional"]
    metric: Literal["total_cost", "lead_time_days", "manual"]
    operator: Literal["lte", "gte", "eq", "manual"]
    threshold: Money | None = None

    @model_validator(mode="after")
    def typed_threshold(self) -> Self:
        if self.metric != "manual" and (self.threshold is None or self.operator == "manual"):
            raise ValueError("A deterministic constraint needs a threshold and operator")
        return self


class Preference(Contract):
    id: Id
    description: ShortText
    rank: int = Field(ge=1, le=10, strict=True)
    origin: Literal["user_supplied", "assumed", "fictional"]


class PublicTerm(Contract):
    id: Id
    text: str = Field(min_length=1, max_length=120)

    @field_validator("text")
    @classmethod
    def approved_search_text(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("Public terms must be nonblank text without control characters")
        return value


class DraftBrief(Contract):
    schema_version: Literal["1.0"] = "1.0"
    revision: int = Field(ge=1, strict=True)
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(max_length=4000)
    background: str = Field(default="", max_length=12000)
    goal: str = Field(max_length=4000)
    currency: Currency
    budget: Money | None = None
    deadline: date | None = None
    timezone: str = Field(default="America/Phoenix", max_length=64)
    location: str = Field(default="", max_length=500)
    horizon_months: int | None = Field(default=None, ge=1, le=120, strict=True)
    options: list[Option] = Field(default_factory=list, max_length=5)
    hard_requirements: list[Constraint] = Field(default_factory=list, max_length=20)
    preferences: list[Preference] = Field(default_factory=list, max_length=10)
    user_facts: list[ShortText] = Field(default_factory=list, max_length=30)
    selected_attachment_ids: list[Id] = Field(default_factory=list, max_length=5)
    public_research_terms: list[PublicTerm] = Field(default_factory=list, max_length=8)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Use an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def uniqueness_and_scope(self) -> Self:
        if len(set(self.selected_attachment_ids)) != len(self.selected_attachment_ids):
            raise ValueError("Selected attachment IDs must be unique")
        for items in (self.options, self.hard_requirements, self.preferences, self.public_research_terms):
            if len({item.id for item in items}) != len(items):
                raise ValueError("IDs within a collection must be unique")
        if sum(option.role == "candidate" for option in self.options) > 4:
            raise ValueError("At most four candidate options are supported")
        if sum(option.role == "status_quo" for option in self.options) > 1:
            raise ValueError("At most one additional status-quo baseline is supported")
        if len({item.rank for item in self.preferences}) != len(self.preferences):
            raise ValueError("Preference ranks must be unique")
        for option in self.options:
            if len({component.id for component in option.cost_components}) != len(option.cost_components):
                raise ValueError("Cost component IDs must be unique")
        return self
