"""Authoritative JSON boundaries. Quantities and money never accept JSON numbers."""

import re
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, json_schema_serialization_defaults_required=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def database_safe_text(cls, value):
        # JSON permits escaped NULs, but PostgreSQL text/JSON does not. Apply
        # the same boundary to SQLite fixtures, nested free-form dictionaries,
        # and provider results before a command or result can be committed.
        def check(item):
            if isinstance(item, str):
                if "\x00" in item:
                    raise ValueError("Text must not contain NUL characters")
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError("Text must contain valid Unicode characters") from exc
            elif isinstance(item, dict):
                for key, child in item.items():
                    check(key)
                    check(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    check(child)
        check(value)
        return value


Id = Annotated[str, Field(min_length=3, max_length=96, pattern=r"^[a-z][a-z0-9_:-]+$")]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


def checked_decimal(value: object, *, maximum: str, places: int, positive: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError("Use a plain nonnegative decimal string, at most 32 characters; exponent notation is forbidden")
    fractional = value.partition(".")[2]
    significant = value.replace(".", "").lstrip("0") or "0"
    if len(significant) > 18 or len(fractional) > places:
        raise ValueError(f"At most 18 significant digits and {places} decimal places are supported")
    amount = Decimal(value)
    if not amount.is_finite() or amount > Decimal(maximum) or (positive and amount <= 0):
        raise ValueError("Decimal operand is outside the supported bounds")
    return value


def money(value: object) -> str:
    return checked_decimal(value, maximum="1000000000000", places=6)


def quantity(value: object) -> str:
    return checked_decimal(value, maximum="1000000000", places=6)


def rate(value: object) -> str:
    return checked_decimal(value, maximum="1000000", places=12, positive=True)


def total_money(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?", value):
        raise ValueError("A calculated monetary result must be a bounded plain decimal string")
    if Decimal(value) > Decimal("1e30"):
        raise ValueError("Calculated money exceeds the 10^30 intermediate bound")
    return value


Money = Annotated[str, BeforeValidator(money), Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")]
Quantity = Annotated[str, BeforeValidator(quantity), Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")]
Rate = Annotated[str, BeforeValidator(rate), Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")]
TotalMoney = Annotated[str, BeforeValidator(total_money), Field(max_length=64, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")]
