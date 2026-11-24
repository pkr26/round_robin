"""Serialized visual workspace contracts; private storage locators stay server-side."""
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from round_robin.schemas.common import Contract, Hash, Id, Money, ShortText
from round_robin.schemas.runtime import RuntimeCost
from .iterations import VisualState
from .schemas import Critique, VisualBrief, VisualProvider
from .assets import MAX_PIXELS


def public_result(value):
    """Read-only adaptation also protects legacy command receipts containing paths."""
    if isinstance(value, list):
        return [public_result(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: public_result({name: item for name, item in child.items() if name not in {"svg_path", "source_path"}} if key == "provenance" and isinstance(child, dict) else child) for key, child in value.items()}


class VisualCoverageView(Contract):
    eligible_critic_providers: list[VisualProvider] = Field(max_length=3)
    complete: bool
    generator_provenance: Literal["known", "unknown"]
    limitations: list[ShortText] = Field(max_length=10)


class DesignReviewView(Critique):
    pass


class RegressionCheckView(Contract):
    provider: VisualProvider
    result: Literal["resolved", "persistent", "regressed", "uncertain", "declined"]
    reason: ShortText


class RegressionDispositionView(Contract):
    id: Id
    kind: Literal["prior_finding", "locked_requirement"]
    description: ShortText
    severity: Literal["material", "critical"]
    status: Literal["pending", "resolved", "persistent", "regressed", "uncertain", "declined"]
    checks: list[RegressionCheckView] = Field(default_factory=list, max_length=3)


class RegressionReviewView(Contract):
    complete: bool = False
    pending_target_ids: list[Id] = Field(default_factory=list, max_length=60)
    unresolved_blockers: list[Id] = Field(default_factory=list, max_length=120)
    dispositions: list[RegressionDispositionView] = Field(default_factory=list, max_length=60)


class DesignVersionView(Contract):
    id: Id
    version: int = Field(ge=1, le=3)
    parent_version_id: Id | None
    canonical_hash: Hash
    original_hash: Hash
    # Earlier uploads checked total pixels but omitted the per-axis bound.
    # Preserve truthful access to immutable history; new inputs enforce8192.
    width: int = Field(ge=1, le=MAX_PIXELS)
    height: int = Field(ge=1, le=MAX_PIXELS)
    generator_provider: VisualProvider | None
    source_kind: Literal["upload", "generated", "edit", "native_diagram", "native_interface"]
    provenance: dict[str, Any]
    image_url: str = Field(pattern=r"^/api/design-versions/[a-z0-9_:-]+/image$")
    reviews: list[DesignReviewView] = Field(max_length=3)
    review_supplements: list[DesignReviewView] = Field(default_factory=list, max_length=3)
    coverage: VisualCoverageView
    regression_review: RegressionReviewView = Field(default_factory=RegressionReviewView)

    @model_validator(mode="after")
    def bounded_saved_pixels(self) -> Self:
        if self.width * self.height > MAX_PIXELS:
            raise ValueError("Saved raster exceeds the pixel bound")
        return self

    @field_validator("provenance", mode="before")
    @classmethod
    def private_paths(cls, value):
        return {key: child for key, child in value.items() if key not in {"svg_path", "source_path"}}


class VisualOperationState(Contract):
    kind: Literal["generation", "edit", "vision"]
    step_id: str = Field(min_length=1, max_length=256)
    bound: RuntimeCost
    actual: RuntimeCost | None
    status: Literal["authorized", "unknown", "settled"]
    authorized_at: float = Field(ge=0, allow_inf_nan=False)
    published_version_id: Id | None = None


class VisualStateView(Contract):
    mode: Literal["review_only", "improve"]
    maximum_usd: Money
    admitted_at: float = Field(ge=0, allow_inf_nan=False)
    request_count: int = Field(ge=0, le=24)
    submission_count: int = Field(ge=0, le=4)
    poll_count: int = Field(ge=0, le=60)
    version_count: int = Field(ge=0, le=3)
    intentional_edits: int = Field(ge=0, le=2)
    renderer_attempts: int = Field(ge=0, le=6)
    paid_admitted: bool
    fence: int = Field(ge=0)
    epoch: int = Field(ge=1)
    status: Literal["active", "cancelled", "completed", "quarantined", "deleted", "expired"]
    step_calls: dict[str, Annotated[int, Field(ge=0, le=2)]] = Field(max_length=24)
    operations: dict[Id, VisualOperationState] = Field(max_length=24)

    @model_validator(mode="after")
    def conserved_counters(self) -> Self:
        VisualState(**self.model_dump(mode="python"))
        return self


class PendingVisualOperation(Contract):
    id: Id
    kind: Literal["generation", "edit", "vision"]
    status: Literal["queued", "claimed", "authorized", "running"]


class VisualOperationFailure(Contract):
    id: Id
    kind: Literal["generation", "edit", "vision"]
    error: ShortText


class DesignSessionView(Contract):
    id: Id
    meeting_id: Id
    mode: Literal["review_only", "improve"]
    status: Literal["active", "cancelled", "completed", "quarantined", "deleted", "expired"]
    revision: int = Field(ge=1)
    brief: VisualBrief
    recipients: list[VisualProvider] = Field(max_length=3)
    selected_version_id: Id | None
    selection_recorded: bool
    versions: list[DesignVersionView] = Field(max_length=3)
    state: VisualStateView
    pending_operations: list[PendingVisualOperation] = Field(max_length=24)
    operation_failures: list[VisualOperationFailure] = Field(max_length=24)
    paid_access_enabled: bool
    limitations: list[ShortText] = Field(max_length=10)

    @model_validator(mode="after")
    def version_and_selection_scope(self) -> Self:
        if self.state.mode != self.mode or self.state.status != self.status or self.state.version_count != len(self.versions):
            raise ValueError("Visual mode/status/version counters must agree with saved candidates")
        if self.selected_version_id is not None and self.selected_version_id not in {version.id for version in self.versions}:
            raise ValueError("Selected version must belong to this design")
        return self


class DesignPage(Contract):
    items: list[DesignSessionView] = Field(max_length=30)


class VisualUploadView(DesignSessionView):
    upload_status: Literal["processing", "ready", "failed", "expired"]


class AcceptedDesignView(DesignSessionView):
    accepted_operation_ids: list[Id] = Field(max_length=2)


class NativeRenderView(DesignSessionView):
    render_status: Literal["pending", "ready", "failed", "expired"]


class VisualCapabilitiesView(Contract):
    enabled: bool
    paid_access_enabled: bool
    raster_formats: list[Literal["image/png", "image/jpeg"]] = Field(max_length=2)
    maximum_upload_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    maximum_pixels: int = Field(gt=0, le=16_000_000)
    maximum_references: int = Field(ge=1, le=4)
    versions: Literal[3]
    image_submissions: Literal[4]
    shared_inference_calls: Literal[24]
    status_polls: Literal[60]
    active_seconds: Literal[1800]
    critics_required: Literal[2]
    live_image_provider_configured: bool
    maximum_usd: Money
    recipients: list[VisualProvider] = Field(max_length=3)
    models: dict[VisualProvider, ShortText]
    generator_provider: VisualProvider | None
    generator_model: ShortText | None
    supported_sizes: list[Annotated[str, Field(pattern=r"^[1-9][0-9]{1,3}x[1-9][0-9]{1,3}$")]] = Field(max_length=8)
    owner_storage_bytes: int = Field(gt=0)
    deployment_storage_bytes: int = Field(gt=0)
    native_diagram_enabled: bool
    native_interface_enabled: bool
    native_diagram_renderer: Literal["structured_source_only"]
    executable_interface_renderer: Literal["disabled", "isolated_docker"]
    limitations: list[ShortText] = Field(max_length=10)


class VisualLimitsView(Contract):
    maximum_usd: Money
    maximum_versions: Literal[3]
    image_submissions: Literal[4]
    shared_inference_calls: Literal[24]
    polls: Literal[60]
    active_seconds: Literal[1800]


class VisualPreflightView(Contract):
    id: Id
    revision: int = Field(ge=1)
    mode: Literal["review_only", "improve"]
    action: Literal["review", "generate", "edit"]
    input_hashes: list[Hash] = Field(max_length=3)
    recipients: list[VisualProvider] = Field(max_length=3)
    brief_hash: Hash
    configuration_hash: Hash | None
    models: dict[VisualProvider, ShortText]
    generator_model: ShortText | None
    created_at: AwareDatetime
    expires_at: AwareDatetime
    limits: VisualLimitsView
    blockers: list[ShortText] = Field(max_length=20)
    paid_access_enabled: bool
    consumed_by_command: UUID | None
