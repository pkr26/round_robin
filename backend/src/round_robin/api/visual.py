"""Optional owner-scoped raster workspace; live visual inference fails closed."""
from dataclasses import asdict
from datetime import datetime, timedelta
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from starlette.requests import ClientDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid import UUID

from round_robin.database.models import Command, utcnow
from round_robin.database.services import canonical_hash, lock_policies, new_id
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.visual.assets import MAX_UPLOAD_BYTES, UnsafeRaster, validate_raster, verified_bytes
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.renderers.diagram import Diagram, render_diagram
from round_robin.visual.renderers.interface import InterfaceAction, IsolatedRendererRequired, NativeRendererError, render_interface
from round_robin.visual.schemas import CreateDesign, DesignCommand, ReviewDesign, SelectDesign
from round_robin.visual.schemas import DesignPreflight, ImproveDesign
from round_robin.visual.views import AcceptedDesignView, DesignPage, DesignSessionView, NativeRenderView, VisualCapabilitiesView, VisualPreflightView, VisualUploadView
from round_robin.visual.service import commit_version, design_view, owned_asset_bytes, queue_version_reviews, release_unused_visual_finish, remove_unreferenced_assets, reserve_asset_storage, save_interface_source, save_native_sources, state_for
from pydantic import Field
from round_robin.providers.images import ImageFundingLocked, load_image_configuration
from round_robin.orchestration.budget import _exact_money
from .auth import get_db, require_owner
from .commands import aware, command_begin, command_commit, fail, owned_meeting

router = APIRouter()


class NativeDiagramCommand(DesignCommand):
    diagram: Diagram


class NativeInterfaceCommand(DesignCommand):
    source: str = Field(min_length=1, max_length=128 * 1024)
    actions: list[InterfaceAction] = Field(default_factory=list, max_length=20)


def configured_images(settings):
    try:
        return load_image_configuration(settings)
    except (ImageFundingLocked, ValueError, OSError):
        return {}


def current_visual_configuration(design, settings):
    current = configured_images(settings)
    if not current or not design.configuration or current.get("operator_configuration_hash") != design.configuration.get("operator_configuration_hash"):
        fail("visual_configuration_changed", "The funded visual configuration is unavailable or changed; review a fresh scope before dispatch")
    return design.configuration


def visual_enabled():
    if os.environ.get("ROUND_ROBIN_VISUAL_ENABLED") != "1":
        fail("visual_disabled", "Visual workspace is disabled until its separate demand and access decision", 404)


def owned_design(session: Session, identity: str, owner: str, *, lock=False):
    if lock:
        lock_policies(session, owner)
    query = select(DesignSession).where(DesignSession.id == identity, DesignSession.owner_id == owner)
    row = session.scalar(query.execution_options(populate_existing=True))
    if row is None:
        fail("not_found", "Design session not found", 404)
    owned_meeting(session, row.meeting_id, owner, lock=lock)
    if lock:
        row = session.scalar(query.with_for_update().execution_options(populate_existing=True))
    return row


@router.get("/api/visual/capabilities", response_model=VisualCapabilitiesView)
def visual_capabilities(request: Request, owner=Depends(require_owner)):
    enabled = os.environ.get("ROUND_ROBIN_VISUAL_ENABLED") == "1"
    configuration = configured_images(request.app.state.settings) if enabled else {}
    return {"enabled": enabled, "paid_access_enabled": bool(configuration),
            "raster_formats": ["image/png", "image/jpeg"], "maximum_upload_bytes": MAX_UPLOAD_BYTES,
            "maximum_pixels": 16_000_000, "maximum_references": 4, "versions": 3,
            "image_submissions": 4, "shared_inference_calls": 24, "status_polls": 60,
            "active_seconds": 1800, "critics_required": 2, "live_image_provider_configured": bool(configuration),
            "maximum_usd": configuration.get("maximum_usd", "0"),
            "recipients": sorted(set(configuration.get("critic_providers", []) + ([configuration["generator_provider"]] if configuration else []))),
            "models": configuration.get("models", {}), "generator_provider": configuration.get("generator_provider"),
            "generator_model": configuration.get("generator_model"),
            "supported_sizes": configuration.get("supported_sizes", []),
            "owner_storage_bytes": 256 * 1024 * 1024, "deployment_storage_bytes": 1024 * 1024 * 1024,
            "native_diagram_enabled": enabled and os.environ.get("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED") == "1",
            "native_interface_enabled": enabled and os.environ.get("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED") == "1" and bool(os.environ.get("ROUND_ROBIN_NATIVE_RENDERER_IMAGE")),
            "native_diagram_renderer": "structured_source_only", "executable_interface_renderer": "isolated_docker" if enabled and os.environ.get("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED") == "1" and os.environ.get("ROUND_ROBIN_NATIVE_RENDERER_IMAGE") else "disabled",
            "limitations": ["Demand and human quality evaluation remain pending."] + ([] if configuration else ["No live visual dispatch is configured."])}


@router.post("/api/meetings/{meeting_id}/design-sessions", status_code=201, response_model=DesignSessionView)
def create_design(meeting_id: str, body: CreateDesign, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "create_design", {"meeting_id": meeting_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        meeting = owned_meeting(session, meeting_id, owner, lock=True)
        existing = list(session.scalars(select(DesignSession).where(DesignSession.owner_id == owner, DesignSession.status != "deleted")))
        if len(existing) >= 30:
            fail("quota_exceeded", "Visual session limit reached", 429)
        configuration = configured_images(request.app.state.settings)
        if Decimal(body.maximum_usd) > 0 and not configuration:
            fail("spending_locked", "This visual workspace has no funded image/vision configuration")
        if configuration and Decimal(body.maximum_usd) > Decimal(configuration["maximum_usd"]):
            fail("visual_allowance_invalid", "The requested visual allowance exceeds the configured operator limit")
        state = VisualState(body.mode, body.maximum_usd, utcnow().timestamp(), epoch=request.app.state.settings.deployment_epoch, paid_admitted=False)
        row = DesignSession(id=new_id("design"), meeting_id=meeting_id, owner_id=owner, mode=body.mode,
                            status="active", revision=1, execution_fence=meeting.execution_fence,
                            deployment_epoch=request.app.state.settings.deployment_epoch,
                            frozen_brief=body.brief.model_dump(mode="json"), recipients=sorted(set(body.recipients)),
                            maximum_usd=body.maximum_usd, configuration=configuration, state={**asdict(state), "fence": meeting.execution_fence})
        session.add(row)
        session.flush()
        result = design_view(session, row)
        command_commit(session, request, owner, body.command_id, "create_design", result, resource_id=row.id, meeting_id=meeting_id, status_code=201)
        return result


@router.get("/api/meetings/{meeting_id}/design-sessions", response_model=DesignPage)
def list_designs(meeting_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    owned_meeting(session, meeting_id, owner)
    rows = session.scalars(select(DesignSession).where(DesignSession.meeting_id == meeting_id, DesignSession.owner_id == owner).order_by(DesignSession.created_at.desc()).limit(30))
    return {"items": [design_view(session, row) for row in rows]}


@router.get("/api/design-sessions/{session_id}", response_model=DesignSessionView)
def read_design(session_id: str, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    return design_view(session, owned_design(session, session_id, owner))


@router.post("/api/design-sessions/{session_id}/images", status_code=201, response_model=VisualUploadView)
async def upload_original(session_id: str, request: Request, x_command_id: UUID = Header(), x_expected_revision: int = Header(), x_content_sha256: str = Header(), owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    media = request.headers.get("content-type", "").split(";")[0]
    if media not in {"image/png", "image/jpeg"}:
        fail("unsupported_media", "Upload actual PNG or JPEG bytes", 415)
    try:
        declared_size = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared_size = 0
    if not 0 < declared_size <= MAX_UPLOAD_BYTES or len(x_content_sha256) != 64 or any(char not in "abcdef0123456789" for char in x_content_sha256):
        fail("invalid_raster", "Provide bounded bytes and their SHA-256 before upload", 422)
    reservation_id = f"visualupload_{x_command_id.hex}"
    with session.begin():
        prior = command_begin(session, request, owner, x_command_id, "upload_design", {"session_id": session_id, "expected_revision": x_expected_revision, "sha256": x_content_sha256, "byte_size": declared_size})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        if design.revision != x_expected_revision or design.status != "active":
            fail("design_conflict", "Refresh this active design's revision before uploading")
        if session.scalar(select(DesignVersion.id).where(DesignVersion.session_id == design.id)):
            fail("original_exists", "An original is already saved; upload cannot overwrite V1")
        try:
            reserve_asset_storage(session, design, reservation_id)
        except VisualLimit as error:
            fail("storage_quota", str(error), 429)
        command_commit(session, request, owner, x_command_id, "upload_design", {**design_view(session, design), "upload_status": "processing"}, resource_id=design.id, meeting_id=design.meeting_id, status_code=201)
    data = bytearray()
    failure = None
    raster = None
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise UnsafeRaster("Image input exceeds ten MiB")
        if len(data) != declared_size:
            raise UnsafeRaster("Upload body is incomplete")
        raster = validate_raster(bytes(data), media)
        if raster.original_hash != x_content_sha256:
            raise UnsafeRaster("Image bytes differ from their declared hash")
    except (UnsafeRaster, ValueError, TimeoutError, ClientDisconnect) as error:
        failure = str(error) or "Raster upload was interrupted or timed out"
    except BaseException:
        # Client cancellation can bypass a normal response. Release this known
        # unused byte hold when possible; the durable reaper handles process/DB
        # loss and fences any late commit against the released hold.
        with session.begin():
            lock_policies(session, owner)
            reservation = session.get(DesignOperation, reservation_id)
            if reservation and reservation.payload.get("status") == "reserved":
                reservation.payload = {**reservation.payload, "status": "released"}
        raise
    with session.begin():
        # Establish a fresh authority snapshot after bytes/decode. An earlier
        # identity-map copy must not resurrect a concurrently deleted meeting.
        design = owned_design(session, session_id, owner, lock=True)
        reservation = session.get(DesignOperation, reservation_id)
        if failure:
            reservation.payload = {**reservation.payload, "status": "released"}
            result = {**design_view(session, design), "upload_status": "failed"}
        else:
            try:
                commit_version(session, design, raster, request.app.state.settings.artifact_directory / "visual", expected_fence=design.execution_fence, storage_reservation_id=reservation_id)
                result = {**design_view(session, design), "upload_status": "ready"}
            except VisualLimit as error:
                reservation.payload = {**reservation.payload, "status": "released"}
                failure = str(error)
                result = {**design_view(session, design), "upload_status": "failed"}
        session.get(Command, (owner, str(x_command_id))).result = result
    if failure:
        fail("invalid_raster", failure, 422)
    return result


@router.get("/api/design-versions/{version_id}/image")
def image(version_id: str, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    version = session.get(DesignVersion, version_id)
    if version is None:
        fail("not_found", "Image not found", 404)
    design = owned_design(session, version.session_id, owner)
    try:
        data = owned_asset_bytes(version, design, request.app.state.settings.artifact_directory)
    except (UnsafeRaster, VisualLimit, OSError):
        fail("artifact_unavailable", "The canonical saved image is unavailable or corrupt", 503)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/api/design-sessions/{session_id}/preflight", response_model=VisualPreflightView)
@_exact_money
def design_preflight(session_id: str, body: DesignPreflight, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "design_preflight", {"session_id": session_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        if design.revision != body.expected_revision:
            fail("design_conflict", "The design changed before preflight")
        versions = list(session.scalars(select(DesignVersion).where(DesignVersion.session_id == design.id).order_by(DesignVersion.version)))
        now = utcnow()
        configuration = configured_images(request.app.state.settings)
        blockers = []
        if design.status != "active":
            blockers.append("This visual session is no longer active. Create a new explicitly authorized visual brief.")
        if any(max(version.width, version.height) > 8192 for version in versions):
            blockers.append("A historical image exceeds the current dimension limit. Resize it and create a new visual brief.")
        if not configuration:
            blockers.append("No live vision/image provider or funded shared-ledger gateway is configured.")
        else:
            if state_for(design).paid_admitted and design.configuration.get("operator_configuration_hash") != configuration.get("operator_configuration_hash"):
                blockers.append("The operator configuration changed after admission; use a new explicitly authorized design scope.")
            else:
                # The user's smaller allowance is stored separately. Mutating
                # operator configuration would invalidate its frozen hash and
                # cause every sub-ceiling allowance to fail worker admission.
                design.configuration = configuration
            if Decimal(design.maximum_usd) > Decimal(configuration["maximum_usd"]):
                blockers.append("The original visual allowance exceeds the current operator limit.")
            critics = configuration.get("critic_providers", [])
            required = set(critics) | ({configuration.get("generator_provider")} if design.mode == "improve" else set())
            if not required <= set(design.recipients):
                blockers.append("The frozen recipients do not permit every configured critic/generator.")
            # A recovery review schedules only missing critics on this version.
            # Their committed peers already appear in exposure and must not be
            # priced a second time. A new generated/edited version needs both.
            from round_robin.visual.service import complete_version_critics
            reviewed = complete_version_critics(session, design, versions[-1]) if body.action == "review" and versions else set()
            review_cost = sum((Decimal(configuration["vision_prices"][provider]) for provider in critics if provider not in reviewed), Decimal("0"))
            extra = Decimal(configuration["generation_price_usd"] if body.action == "generate" else configuration["edit_price_usd"] if body.action == "edit" else "0")
            if state_for(design).exposure() + review_cost + extra > Decimal(design.maximum_usd):
                blockers.append("The original allowance cannot fund this action and its protected independent reviews.")
            if body.action in {"generate", "edit"}:
                size = f"{design.frozen_brief['width']}x{design.frozen_brief['height']}"
                sizes = configuration.get("supported_sizes", [])
                if size not in sizes:
                    blockers.append(f"The frozen dimensions {size} are unsupported by the configured generator. Create a brief using one of: {', '.join(sizes)}.")
        if body.action == "review" and not versions:
            blockers.append("Supply an original image before review.")
        if body.action != "review" and design.mode != "improve":
            blockers.append("Review-only mode does not authorize generation or editing.")
        if body.action == "generate" and versions:
            blockers.append("Generation creates V1 only; later candidates are intentional edits.")
        if body.action == "edit" and (not versions or len(versions) >= 3):
            blockers.append("Editing requires an existing candidate and remaining V2/V3 allowance.")
        if any(operation.payload.get("status") in {"queued", "claimed", "authorized", "running"} for operation in session.scalars(select(DesignOperation).where(DesignOperation.session_id == design.id))):
            blockers.append("Previously accepted visual work is still pending.")
        payload = {"id": new_id("designpreflight"), "revision": design.revision, "mode": design.mode, "action": body.action,
                   "input_hashes": [row.canonical_hash for row in versions], "recipients": design.recipients,
                   "brief_hash": canonical_hash(design.frozen_brief), "configuration_hash": design.configuration.get("operator_configuration_hash"),
                   "models": design.configuration.get("models", {}), "generator_model": design.configuration.get("generator_model"),
                   "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat(),
                   "limits": {"maximum_usd": design.maximum_usd, "maximum_versions": 3, "image_submissions": 4, "shared_inference_calls": 24, "polls": 60, "active_seconds": 1800},
                   "blockers": blockers, "paid_access_enabled": bool(configuration), "consumed_by_command": None}
        design.preflight = payload
        command_commit(session, request, owner, body.command_id, "design_preflight", payload, resource_id=design.id, meeting_id=design.meeting_id)
        return payload


def consume_visual_preflight(session: Session, design: DesignSession, body: ReviewDesign, request: Request, action: str):
    preflight = design.preflight
    if not preflight or preflight["id"] != body.preflight_id or design.revision != body.expected_revision or preflight.get("action") != action or aware(datetime.fromisoformat(preflight["expires_at"])) <= utcnow() or preflight.get("consumed_by_command"):
        fail("preflight_stale", "Review the current unconsumed visual preflight before starting")
    if preflight["blockers"]:
        fail("visual_funding_locked", preflight["blockers"][0])
    current_visual_configuration(design, request.app.state.settings)
    versions = list(session.scalars(select(DesignVersion).where(DesignVersion.session_id == design.id).order_by(DesignVersion.version)))
    if preflight["input_hashes"] != [row.canonical_hash for row in versions] or preflight["brief_hash"] != canonical_hash(design.frozen_brief):
        fail("preflight_stale", "Frozen image inputs or brief differ from the displayed preflight")
    now = utcnow()
    design.preflight = {**preflight, "consumed_by_command": str(body.command_id), "queue_accepted_at": now.isoformat(),
                        "initial_admission_expires_at": (now + timedelta(minutes=30)).isoformat()}
    return versions


@router.post("/api/design-sessions/{session_id}/reviews", status_code=202, response_model=AcceptedDesignView)
def start_visual_review(session_id: str, body: ReviewDesign, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "start_visual_review", {"session_id": session_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        versions = consume_visual_preflight(session, design, body, request, "review")
        if not versions:
            fail("original_missing", "An actual image is required for visual review")
        try:
            rows = queue_version_reviews(session, design, versions[-1])
        except VisualLimit as error:
            fail("review_unavailable", str(error))
        if not rows:
            fail("review_already_recorded", "No eligible missing critic work remains on this saved version")
        design.revision += 1
        session.flush()
        result = {**design_view(session, design), "accepted_operation_ids": [row.id for row in rows]}
        command_commit(session, request, owner, body.command_id, "start_visual_review", result, resource_id=design.id, meeting_id=design.meeting_id, status_code=202)
        return result


@router.post("/api/design-sessions/{session_id}/improvements", status_code=202, response_model=AcceptedDesignView)
def improve_design(session_id: str, body: ImproveDesign, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "improve_design", {"session_id": session_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        action = "edit" if body.target_version_id else "generate"
        versions = consume_visual_preflight(session, design, body, request, action)
        if design.mode != "improve" or (versions[-1].id if versions else None) != body.target_version_id:
            fail("edit_scope_invalid", "Use the latest immutable version in the frozen Improve mode")
        if versions:
            from round_robin.visual.service import version_view, version_feedback_findings
            current = version_view(session, versions[-1])
            feedback_findings = version_feedback_findings(session, design, versions[-1])
            if not current["coverage"]["complete"]:
                fail("review_incomplete", "Complete independent pixel review before automatic editing")
            findings = {finding["id"] for finding in feedback_findings}
            if not set(body.finding_dispositions) <= findings:
                fail("invalid_finding", "Edit dispositions must refer to findings on the exact parent version")
            if versions[-1].parent_version_id and not current["regression_review"]["complete"]:
                fail("regression_review_incomplete", "Record the pending prior-issue and locked-requirement checks before another edit")
        config = design.configuration
        number = len(versions) + 1
        kind = "edit" if versions else "generation"
        step = f"{kind}:version_{number}"
        if state_for(design).step_calls.get(step, 0) >= 2:
            fail("image_step_exhausted", "This immutable image step has no remaining submission attempt")
        identity = new_id("designoperation")
        payload = {"kind": kind, "step_id": step, "status": "queued", "dispatch_status": "pending",
                   "target_version_id": body.target_version_id, "parent_version_id": body.target_version_id,
                   "input_hash": versions[-1].canonical_hash if versions else None,
                   "owner_id": owner, "session_id": design.id, "fence": design.execution_fence, "epoch": design.deployment_epoch,
                   "bound_usd": config["edit_price_usd" if versions else "generation_price_usd"],
                   "edit_intent": body.intent, "finding_dispositions": body.finding_dispositions,
                   "queue_accepted_at": design.preflight["queue_accepted_at"],
                   "initial_admission_expires_at": design.preflight["initial_admission_expires_at"],
                   "review_pool_tag": step}
        if versions:
            from round_robin.visual.feedback import build_edit_bundle
            bundle, bundle_hash = build_edit_bundle(brief=design.frozen_brief, parent_version_id=versions[-1].id,
                parent_hash=versions[-1].canonical_hash,
                findings=feedback_findings,
                dispositions=body.finding_dispositions, intent=body.intent)
            payload.update(edit_bundle=bundle, edit_bundle_hash=bundle_hash)
        payload["request_hash"] = canonical_hash({"payload": payload, "brief": design.frozen_brief, "configuration": config["operator_configuration_hash"]})
        row = DesignOperation(id=identity, session_id=design.id, provider=config["generator_provider"], payload=payload)
        session.add(row)
        design.revision += 1
        session.flush()
        result = {**design_view(session, design), "accepted_operation_ids": [identity]}
        command_commit(session, request, owner, body.command_id, "improve_design", result, resource_id=design.id, meeting_id=design.meeting_id, status_code=202)
        return result


@router.post("/api/design-sessions/{session_id}/selection", response_model=DesignSessionView)
def select_design(session_id: str, body: SelectDesign, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "select_design", {"session_id": session_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        if design.revision != body.expected_revision:
            fail("design_conflict", "The candidate selection changed; refresh first")
        version = session.get(DesignVersion, body.version_id) if body.version_id else None
        if body.version_id and (not version or version.session_id != design.id):
            fail("foreign_design_version", "Select a version from this design only", 404)
        design.selected_version_id, design.selection_recorded = body.version_id, True
        design.revision += 1
        result = design_view(session, design)
        command_commit(session, request, owner, body.command_id, "select_design", result, resource_id=design.id, meeting_id=design.meeting_id)
        return result


@router.post("/api/design-sessions/{session_id}/cancel", response_model=DesignSessionView)
def cancel_design(session_id: str, body: DesignCommand, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "cancel_design", {"session_id": session_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        if design.revision != body.expected_revision:
            fail("design_conflict", "Refresh this visual session before cancelling")
        state = state_for(design)
        state.cancel()
        design.status, design.execution_fence, design.state = "cancelled", state.fence, asdict(state)
        release_unused_visual_finish(session, design, "user_cancellation")
        design.revision += 1
        result = design_view(session, design)
        command_commit(session, request, owner, body.command_id, "cancel_design", result, resource_id=design.id, meeting_id=design.meeting_id)
        return result


@router.post("/api/design-sessions/{session_id}/native-diagram", status_code=201, response_model=NativeRenderView)
def native_diagram(session_id: str, body: NativeDiagramCommand, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    if os.environ.get("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED") != "1":
        fail("native_renderer_disabled", "Native diagram rendering has a separate explicit domain gate", 404)
    reservation_id = f"visualrender_{body.command_id.hex}"
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "render_native_diagram", {"session_id": session_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        if design.revision != body.expected_revision or design.status != "active" or design.frozen_brief.get("domain") != "structured_diagram":
            fail("native_scope_invalid", "Use the active frozen structured-diagram scope and current revision")
        if (body.diagram.width, body.diagram.height) != (design.frozen_brief["width"], design.frozen_brief["height"]):
            fail("native_dimensions_mismatch", "Diagram dimensions must match the frozen visual brief", 422)
        state = state_for(design)
        if state.renderer_attempts >= 6 or state.version_count >= 3 or state.version_count > 0 and design.mode != "improve":
            fail("renderer_limit", "Native render/immutable version allowance is exhausted")
        labels = "\n".join([node.label for node in body.diagram.nodes] + [edge.label for edge in body.diagram.edges])
        if any(text not in labels for text in design.frozen_brief.get("required_text", [])):
            fail("required_text_missing", "Native source omits required literal text", 422)
        try:
            reserve_asset_storage(session, design, reservation_id)
        except VisualLimit as error:
            fail("storage_quota", str(error), 429)
        state.renderer_attempts += 1
        design.state = asdict(state)
        command_commit(session, request, owner, body.command_id, "render_native_diagram", {**design_view(session, design), "render_status": "pending"}, resource_id=design.id, meeting_id=design.meeting_id, status_code=201)
    try:
        rendered = render_diagram(body.diagram)
    except (ValueError, OSError):
        with session.begin():
            reservation = session.get(DesignOperation, reservation_id)
            reservation.payload = {**reservation.payload, "status": "released"}
            design = owned_design(session, session_id, owner)
            session.get(Command, (owner, str(body.command_id))).result = {**design_view(session, design), "render_status": "failed"}
        fail("native_render_failed", "Native output could not be rendered; no candidate is ready", 422)
    failure = None
    with session.begin():
        design = owned_design(session, session_id, owner, lock=True)
        try:
            # Roll back publication as one unit if the revision changed or any
            # native source cannot be saved. The original receipt must still
            # resolve and release its unused byte hold after a conflict.
            with session.begin_nested():
                if design.revision != body.expected_revision:
                    raise VisualLimit("Design changed during rendering; no stale output may publish")
                latest = session.scalar(select(DesignVersion).where(DesignVersion.session_id == design.id).order_by(DesignVersion.version.desc()).limit(1))
                root = request.app.state.settings.artifact_directory / "visual"
                version = commit_version(session, design, rendered.raster, root, expected_fence=design.execution_fence,
                                         parent_version_id=latest.id if latest else None, source_kind="native_diagram", storage_reservation_id=reservation_id)
                save_native_sources(session, design, version, rendered, root)
        except VisualLimit as error:
            failure = str(error)
            remove_unreferenced_assets(session, design, request.app.state.settings.artifact_directory)
            reservation = session.get(DesignOperation, reservation_id, populate_existing=True)
            reservation.payload = {**reservation.payload, "status": "released"}
        result = {**design_view(session, design), "render_status": "failed" if failure else "ready"}
        session.get(Command, (owner, str(body.command_id))).result = result
    if failure:
        fail("native_scope_changed", failure)
    return result


@router.get("/api/design-versions/{version_id}/source.svg")
def native_source(version_id: str, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    version = session.get(DesignVersion, version_id)
    if version is None:
        fail("not_found", "Native candidate not found", 404)
    design = owned_design(session, version.session_id, owner)
    if version.source_kind != "native_diagram" or not version.provenance.get("svg_path"):
        fail("native_source_unavailable", "This candidate has no editable native diagram source", 404)
    try:
        path = Path(version.provenance["svg_path"])
        if path.stat().st_size > 1024 * 1024:
            raise ValueError()
        data = owned_asset_bytes(version, design, request.app.state.settings.artifact_directory, native_source=True)
    except (OSError, ValueError):
        fail("native_source_unavailable", "Native source is unavailable or failed its hash check", 503)
    return Response(data, media_type="image/svg+xml", headers={"Content-Disposition": f'attachment; filename="diagram-v{version.version}.svg"', "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; font-src data:", "X-Content-Type-Options": "nosniff"})


@router.get("/api/design-versions/{version_id}/source.json", response_model=Diagram)
def native_diagram_json(version_id: str, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    version = session.get(DesignVersion, version_id)
    if version is None:
        fail("not_found", "Native candidate not found", 404)
    design = owned_design(session, version.session_id, owner)
    if version.source_kind != "native_diagram" or not version.provenance.get("source_path"):
        fail("native_source_unavailable", "This candidate has no editable diagram source", 404)
    try:
        data = owned_asset_bytes(version, design, request.app.state.settings.artifact_directory, native_source="source")
        if len(data) > 1024 * 1024:
            raise ValueError("source_limit")
        if version.provenance.get("renderer_version") in {"structured-box-diagram-v1", "structured-box-diagram-v2"}:
            # This inert, hash-bound source was published under the original
            # renderer's looser glyph/relationship rules. Preserve access to
            # history; only a new render must satisfy today's publication rules.
            if not isinstance(json.loads(data), dict):
                raise ValueError("source_shape")
        else:
            Diagram.model_validate_json(data)
    except (ValueError, OSError):
        fail("native_source_unavailable", "Native source is unavailable or failed its hash/schema check", 503)
    return Response(data, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="diagram-v{version.version}.json"', "Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"})


@router.post("/api/design-sessions/{session_id}/native-interface", status_code=201, response_model=NativeRenderView)
def native_interface(session_id: str, body: NativeInterfaceCommand, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    if os.environ.get("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED") != "1" or not os.environ.get("ROUND_ROBIN_NATIVE_RENDERER_IMAGE"):
        fail("native_renderer_disabled", "Native interfaces require the explicitly configured isolated renderer", 404)
    reservation_id = f"visualrender_{body.command_id.hex}"
    with session.begin():
        prior = command_begin(session, request, owner, body.command_id, "render_native_interface", {"session_id": session_id, **body.model_dump(mode="json")})
        if prior:
            return prior.result
        design = owned_design(session, session_id, owner, lock=True)
        if design.revision != body.expected_revision or design.status != "active" or design.frozen_brief.get("domain") != "native_interface":
            fail("native_scope_invalid", "Use the active frozen native-interface scope and current revision")
        state = state_for(design)
        if state.renderer_attempts >= 6 or state.version_count >= 3 or state.version_count > 0 and design.mode != "improve":
            fail("renderer_limit", "Native render/immutable version allowance is exhausted")
        if any(text not in body.source for text in design.frozen_brief.get("required_text", [])):
            fail("required_text_missing", "Native source omits required literal text", 422)
        try:
            reserve_asset_storage(session, design, reservation_id)
        except VisualLimit as error:
            fail("storage_quota", str(error), 429)
        state.renderer_attempts += 1
        design.state = asdict(state)
        viewport = (design.frozen_brief["width"], design.frozen_brief["height"])
        command_commit(session, request, owner, body.command_id, "render_native_interface", {**design_view(session, design), "render_status": "pending"}, resource_id=design.id, meeting_id=design.meeting_id, status_code=201)
    try:
        rendered = render_interface(body.source, viewport=viewport, actions=body.actions)
    except (IsolatedRendererRequired, NativeRendererError, ValueError, OSError):
        with session.begin():
            reservation = session.get(DesignOperation, reservation_id)
            reservation.payload = {**reservation.payload, "status": "released"}
            design = owned_design(session, session_id, owner)
            session.get(Command, (owner, str(body.command_id))).result = {**design_view(session, design), "render_status": "failed"}
        fail("native_render_failed", "The isolated renderer is unavailable or the declared checks could not finish; no candidate is ready", 422)
    failure = None
    with session.begin():
        design = owned_design(session, session_id, owner, lock=True)
        try:
            # Roll back publication as one unit if the revision changed or any
            # native source cannot be saved. The original receipt must still
            # resolve and release its unused byte hold after a conflict.
            with session.begin_nested():
                if design.revision != body.expected_revision:
                    raise VisualLimit("Design changed during rendering; no stale output may publish")
                latest = session.scalar(select(DesignVersion).where(DesignVersion.session_id == design.id).order_by(DesignVersion.version.desc()).limit(1))
                root = request.app.state.settings.artifact_directory / "visual"
                version = commit_version(session, design, rendered.raster, root, expected_fence=design.execution_fence,
                                         parent_version_id=latest.id if latest else None, source_kind="native_interface", storage_reservation_id=reservation_id)
                save_interface_source(session, design, version, rendered, root)
                version.provenance = {**version.provenance, "interaction_plan": [action.model_dump(mode="json", exclude_none=True) for action in body.actions]}
        except VisualLimit as error:
            failure = str(error)
            remove_unreferenced_assets(session, design, request.app.state.settings.artifact_directory)
            reservation = session.get(DesignOperation, reservation_id, populate_existing=True)
            reservation.payload = {**reservation.payload, "status": "released"}
        result = {**design_view(session, design), "render_status": "failed" if failure else "ready"}
        session.get(Command, (owner, str(body.command_id))).result = result
    if failure:
        fail("native_scope_changed", failure)
    return result


@router.get("/api/design-versions/{version_id}/source.html")
def native_interface_source(version_id: str, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    visual_enabled()
    version = session.get(DesignVersion, version_id)
    if version is None:
        fail("not_found", "Native candidate not found", 404)
    design = owned_design(session, version.session_id, owner)
    if version.source_kind != "native_interface" or not version.provenance.get("source_path"):
        fail("native_source_unavailable", "This candidate has no editable interface source", 404)
    try:
        data = owned_asset_bytes(version, design, request.app.state.settings.artifact_directory, native_source="source")
        if len(data) > 128 * 1024:
            raise ValueError()
    except (ValueError, OSError):
        fail("native_source_unavailable", "Native source is unavailable or failed its hash check", 503)
    return Response(data, media_type="text/plain", headers={"Content-Disposition": f'attachment; filename="interface-v{version.version}.html"', "Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"})
