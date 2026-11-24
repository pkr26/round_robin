from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from round_robin.schemas.common import Contract, Hash, Id, Money, ShortText

VisualProvider = Literal["openai", "anthropic", "google"]


class VisualBrief(Contract):
    purpose: str = Field(min_length=1, max_length=4000)
    audience: ShortText
    domain: Literal["raster_concept", "structured_diagram", "native_interface"] = "raster_concept"
    required_text: list[ShortText] = Field(default_factory=list, max_length=20)
    locked_requirements: list[ShortText] = Field(default_factory=list, max_length=20)
    width: int = Field(default=1024, ge=1, le=8192, strict=True)
    height: int = Field(default=1024, ge=1, le=8192, strict=True)

    @model_validator(mode="after")
    def bounded_pixels(self) -> Self:
        if self.width * self.height > 16_000_000:
            raise ValueError("Visual brief exceeds sixteen megapixels")
        return self


class CreateDesign(Contract):
    command_id: UUID
    mode: Literal["review_only", "improve"]
    brief: VisualBrief
    recipients: list[VisualProvider] = Field(default_factory=list, max_length=3)
    maximum_usd: Money = "0"


class DesignCommand(Contract):
    command_id: UUID
    expected_revision: int = Field(ge=1, strict=True)


class SelectDesign(DesignCommand):
    version_id: Id | None


class ReviewDesign(DesignCommand):
    preflight_id: Id


class DesignPreflight(DesignCommand):
    action: Literal["review", "generate", "edit"] = "review"


class ImproveDesign(ReviewDesign):
    target_version_id: Id | None = None
    intent: str = Field(min_length=1, max_length=2000)
    finding_dispositions: dict[Id, Literal["address", "decline"]] = Field(default_factory=dict, max_length=20)


class Region(Contract):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def in_bounds(self) -> Self:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Finding region must stay within the supplied pixels")
        return self


class Finding(Contract):
    id: Id
    description: ShortText
    category: Literal["observed_defect", "preference", "unverified_technical_claim"]
    severity: Literal["minor", "material", "critical"]
    region: Region | None = None
    view_index: int = Field(default=0, ge=0, le=3, strict=True)
    previous_finding_id: Id | None = None
    locked_requirement: ShortText | None = None
    disposition: Literal["pending", "addressed", "persistent", "regressed", "declined"] = "pending"


class RegressionCheck(Contract):
    target_id: Id
    result: Literal["resolved", "persistent", "regressed", "uncertain", "declined"]
    reason: ShortText


class Critique(Contract):
    provider: VisualProvider
    version_id: Id
    canonical_hash: Hash
    receipt_id: Id
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    limitations: list[ShortText] = Field(default_factory=list, max_length=10)
    regression_checks: list[RegressionCheck] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def local_finding_aliases(self) -> Self:
        if len({finding.id for finding in self.findings}) != len(self.findings):
            raise ValueError("Finding aliases must be unique within one critic response")
        if len({check.target_id for check in self.regression_checks}) != len(self.regression_checks):
            raise ValueError("Regression targets require one disposition per critic")
        return self


class ImageCapabilities(Contract):
    provider: VisualProvider
    endpoint_version: ShortText
    understands_images: bool = False
    generates_images: bool = False
    edits_images: bool = False
    asynchronous_jobs: bool = False
    job_lookup: bool = False
    idempotent_submission: bool = False
    cancellation: bool = False
    maximum_input_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=64 * 1024 * 1024)
    supported_media: list[Literal["image/png", "image/jpeg"]] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def honest_jobs(self) -> Self:
        if (self.job_lookup or self.cancellation) and not self.asynchronous_jobs:
            raise ValueError("Job lookup/cancellation requires a real asynchronous job API")
        return self
