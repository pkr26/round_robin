from datetime import date
import hashlib
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from .common import Contract, Currency, Hash, Id, ShortText, TotalMoney


class EvidencePassage(Contract):
    id: Id
    owner_id: Id
    run_id: Id
    source_id: Id
    source_version: int = Field(default=1, ge=1, strict=True)
    source_title: ShortText
    locator: ShortText
    text: str = Field(min_length=1, max_length=2000)
    sha256: Hash
    verified_at: AwareDatetime
    effective_date: date | None = None
    origin: Literal["fictional", "user_supplied", "retrieved"]
    verification_status: Literal["fictional", "user_supplied", "not_checked", "supported", "contradicted", "mixed"]
    scope: ShortText
    time_sensitive: bool
    origin_group: Id

    @model_validator(mode="after")
    def exact_passage_hash(self) -> Self:
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("Passage hash must match its exact stored UTF-8 text")
        return self


class CalculationInput(Contract):
    label: ShortText
    amount: str = Field(min_length=1, max_length=64, pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")
    unit: str = Field(min_length=1, max_length=32)
    source_ref: Id


class Calculation(Contract):
    id: Id
    label: ShortText
    operation: Literal["lifecycle_total", "break_even", "add", "subtract", "multiply", "divide", "convert"]
    inputs: list[CalculationInput] = Field(max_length=120)
    formula: str = Field(max_length=2000)
    output: str | None = Field(default=None, max_length=96, pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")
    unit: str = Field(min_length=1, max_length=32)
    currency: Currency | None = None
    horizon_months: int | None = Field(default=None, ge=1, le=120, strict=True)
    rounding: ShortText
    decimal_precision: Literal[64] = 64
    flags: list[ShortText] = Field(default_factory=list, max_length=10)
    # Thirty entered components plus five omitted applicable categories.
    missing_components: list[ShortText] = Field(default_factory=list, max_length=35)
    known_subtotal: TotalMoney | None = None
