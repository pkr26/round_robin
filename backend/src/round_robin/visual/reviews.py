"""Server-issued pixel receipts; model hash echoes never establish inspection."""
from dataclasses import asdict, dataclass
import hashlib
from uuid import uuid4

from .schemas import Critique, ImageCapabilities, VisualBrief
from .assets import validate_canonical_raster


@dataclass(frozen=True)
class PixelView:
    image_bytes: bytes
    sha256: str
    transform: str = "canonical_overview_no_transform"


@dataclass(frozen=True)
class PixelReceipt:
    id: str
    provider: str
    version_id: str
    canonical_hash: str
    views: tuple[dict, ...]
    endpoint_version: str


def issue_review_receipt(capabilities: ImageCapabilities, version_id: str, canonical_hash: str, views: list[PixelView]) -> PixelReceipt:
    if not capabilities.understands_images or "image/png" not in capabilities.supported_media:
        raise ValueError("Configured endpoint cannot inspect the supplied pixels")
    if not 1 <= len(views) <= 4 or views[0].sha256 != canonical_hash:
        raise ValueError("Provide the shared canonical overview and at most three recorded detail views")
    payload = []
    for view in views:
        if not view.image_bytes or len(view.image_bytes) > capabilities.maximum_input_bytes or hashlib.sha256(view.image_bytes).hexdigest() != view.sha256:
            raise ValueError("A server pixel receipt requires the exact bounded outgoing bytes")
        validate_canonical_raster(view.image_bytes)
        payload.append({"sha256": view.sha256, "byte_size": len(view.image_bytes), "transform": view.transform})
    return PixelReceipt(f"receipt_{uuid4().hex}", capabilities.provider, version_id, canonical_hash, tuple(payload), capabilities.endpoint_version)


def independent_review_packet(brief: VisualBrief, receipt: PixelReceipt, views: list[PixelView], *, prior_context: dict | None = None) -> dict:
    # Peer findings are intentionally not an input to an initial critique.
    if len(views) != len(receipt.views):
        raise ValueError("Outgoing pixel views differ from the server receipt")
    for view, recorded in zip(views, receipt.views, strict=True):
        if (hashlib.sha256(view.image_bytes).hexdigest(), len(view.image_bytes), view.transform) != (recorded["sha256"], recorded["byte_size"], recorded["transform"]):
            raise ValueError("Outgoing pixel bytes or transform changed after the server receipt")
        validate_canonical_raster(view.image_bytes)
    packet = {"brief": brief.model_dump(mode="json"), "receipt": asdict(receipt), "images": [view.image_bytes for view in views]}
    if prior_context:
        if len(views) < 2 or prior_context.get("parent_hash") != receipt.views[1]["sha256"] or receipt.views[1]["transform"] != f"comparison_parent_version_{prior_context.get('parent_version_id')}":
            raise ValueError("Shared prior findings require the exact recorded parent pixels")
        findings = prior_context.get("findings", [])
        if not isinstance(findings, list) or len(findings) > 40 or len({item["id"] for item in findings}) != len(findings):
            raise ValueError("Shared prior finding context must be bounded and uniquely referenced")
        if len(str(prior_context.get("edit_intent", ""))) > 2000:
            raise ValueError("Edit intent exceeds the bounded shared context")
        from .feedback import regression_targets, verify_edit_bundle
        targets = regression_targets(brief.model_dump(mode="json"), findings) if "regression_targets" in prior_context else None
        if targets is not None and prior_context["regression_targets"] != targets:
            raise ValueError("Regression target scope changed after the server review")
        if prior_context.get("edit_bundle") is not None:
            verify_edit_bundle(prior_context["edit_bundle"], prior_context.get("edit_bundle_hash"), brief=brief.model_dump(mode="json"),
                parent_version_id=prior_context["parent_version_id"], parent_hash=prior_context["parent_hash"])
        packet["shared_prior_context"] = prior_context
    return packet


def validate_critique(critique: Critique, receipt: PixelReceipt, *, prior_finding_ids: set[str] | None = None) -> Critique:
    if (critique.provider, critique.version_id, critique.canonical_hash, critique.receipt_id) != (receipt.provider, receipt.version_id, receipt.canonical_hash, receipt.id):
        raise ValueError("Critique does not match the actual server-delivered pixel/version receipt")
    if any(finding.view_index >= len(receipt.views) for finding in critique.findings):
        raise ValueError("A finding references a view the critic did not receive")
    if prior_finding_ids is not None and any(finding.previous_finding_id is not None and finding.previous_finding_id not in prior_finding_ids for finding in critique.findings):
        raise ValueError("A regression disposition refers to an unknown prior-version finding")
    return critique


def coverage(critiques: list[Critique], receipts: list[PixelReceipt], *, version_id: str, canonical_hash: str, generator_provider: str | None) -> dict:
    known = {receipt.id: receipt for receipt in receipts}
    providers = set()
    for critique in critiques:
        receipt = known.get(critique.receipt_id)
        if receipt is None:
            continue
        validate_critique(critique, receipt)
        if critique.version_id == version_id and critique.canonical_hash == canonical_hash and critique.provider != generator_provider:
            providers.add(critique.provider)
    return {"eligible_critic_providers": sorted(providers), "complete": len(providers) >= 2,
            "generator_provenance": "unknown" if generator_provider is None else "known",
            "limitations": ["Unknown generator provenance cannot establish independence from the generator."] if generator_provider is None else []}
