"""Immutable design persistence and independent-review commit boundaries."""
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from round_robin.database.models import BudgetOperation, Command, Meeting, utcnow
from round_robin.database.services import lock_policies, new_id
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from .assets import MAX_DIMENSION, Raster, UnsafeRaster, reject_linked_path as _reject_linked_path, store_raster, verified_bytes
from .iterations import VisualLimit, VisualState
from .reviews import PixelReceipt, coverage, validate_critique
from .schemas import Critique, Finding

OWNER_VISUAL_BYTES = 256 * 1024 * 1024
DEPLOYMENT_VISUAL_BYTES = 1024 * 1024 * 1024


def reject_linked_path(path: Path) -> None:
    try:
        _reject_linked_path(path)
    except UnsafeRaster as error:
        raise VisualLimit(str(error)) from error


def checked_private_bytes(path: Path, expected_hash: str, maximum_bytes: int) -> bytes:
    """Open saved private bytes without following links or blocking on devices."""
    try:
        content = verified_bytes(path, expected_hash)
    except (UnsafeRaster, OSError) as error:
        raise VisualLimit("Saved private source failed its checksum or byte contract") from error
    if len(content) > maximum_bytes:
        raise VisualLimit("Saved private source exceeds its byte bound")
    return content


def check_storage_capacity(session: Session, design: DesignSession, additional_bytes: int, *, exclude_reservation: str | None = None) -> None:
    # Share deployment/owner policy locks so competing owners cannot race the
    # deployment-wide storage reservation. Temporary holds count until settled.
    lock_policies(session, design.owner_id)
    designs = {row.id: row for row in session.scalars(select(DesignSession))}
    owner_bytes, global_bytes = 0, 0
    for version in session.scalars(select(DesignVersion)):
        size = int(version.provenance.get("canonical_bytes", 64 * 1024 * 1024)) + int(version.provenance.get("native_bytes", 0))
        global_bytes += size
        if designs[version.session_id].owner_id == design.owner_id:
            owner_bytes += size
    for operation in session.scalars(select(DesignOperation)):
        if operation.id == exclude_reservation or operation.payload.get("kind") != "asset_reservation" or operation.payload.get("status") != "reserved":
            continue
        size = int(operation.payload["maximum_bytes"])
        global_bytes += size
        if designs[operation.session_id].owner_id == design.owner_id:
            owner_bytes += size
    if additional_bytes < 0 or owner_bytes + additional_bytes > OWNER_VISUAL_BYTES or global_bytes + additional_bytes > DEPLOYMENT_VISUAL_BYTES:
        raise VisualLimit("Owner/deployment visual storage including pending reservations is exhausted")


def reserve_asset_storage(session: Session, design: DesignSession, operation_id: str) -> DesignOperation:
    check_storage_capacity(session, design, 64 * 1024 * 1024)
    row = DesignOperation(id=operation_id, session_id=design.id, provider="local",
                          payload={"kind": "asset_reservation", "maximum_bytes": 64 * 1024 * 1024, "status": "reserved"})
    session.add(row)
    return row


def state_for(design: DesignSession) -> VisualState:
    return VisualState(**design.state)


def owned_asset_bytes(version: DesignVersion, design: DesignSession, artifact_directory: Path, *, native_source: bool | str = False) -> bytes:
    from .assets import verified_bytes
    if version.session_id != design.id:
        raise VisualLimit("Saved visual belongs to a different session")
    reject_linked_path(artifact_directory / "visual" / design.owner_id / design.id)
    root = (artifact_directory / "visual" / design.owner_id / design.id).resolve()
    path = Path(version.provenance["source_path"] if native_source == "source" else version.provenance["svg_path"] if native_source else version.asset_path)
    reject_linked_path(path)
    if not path.resolve().is_relative_to(root):
        raise VisualLimit("Saved visual path is outside its private owner/session namespace")
    return verified_bytes(path, version.provenance["source_hash"] if native_source == "source" else version.provenance["svg_hash"] if native_source else version.canonical_hash)


def save_native_sources(session: Session, design: DesignSession, version: DesignVersion, rendered, directory: Path) -> None:
    source = json.dumps(rendered.source, sort_keys=True, separators=(",", ":")).encode()
    check_storage_capacity(session, design, len(source) + len(rendered.svg))
    root = directory / design.owner_id / design.id
    reject_linked_path(root)
    paths = {}
    for label, content, digest, suffix in (("svg_path", rendered.svg, rendered.svg_hash, ".svg"), ("source_path", source, rendered.source_hash, ".json")):
        if len(content) > 1024 * 1024 or hashlib.sha256(content).hexdigest() != digest:
            raise VisualLimit("Native source exceeds its byte/hash contract")
        path = root / f"{digest}{suffix}"
        reject_linked_path(path)
        if path.exists():
            checked_private_bytes(path, digest, 1024 * 1024)
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
        paths[label] = str(path)
    version.provenance = {**version.provenance, **paths, "native_bytes": len(source) + len(rendered.svg),
                          "source_hash": rendered.source_hash, "svg_hash": rendered.svg_hash,
                          "renderer_version": rendered.renderer_version,
                          "source_validation": "bounded nodes, labels, viewport and resolvable relationships",
                          "functional_scope": "structured diagram only; no executable interface or engineering certification"}
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_interface_source(session: Session, design: DesignSession, version: DesignVersion, rendered, directory: Path) -> None:
    content = rendered.source.encode("utf-8")
    if len(content) > 128 * 1024 or hashlib.sha256(content).hexdigest() != rendered.source_hash:
        raise VisualLimit("Native interface source exceeded its hash/byte boundary")
    check_storage_capacity(session, design, len(content))
    root = directory / design.owner_id / design.id
    reject_linked_path(root)
    path = root / f"{rendered.source_hash}.html"
    reject_linked_path(path)
    if path.exists():
        checked_private_bytes(path, rendered.source_hash, 128 * 1024)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    version.provenance = {**version.provenance, "source_path": str(path), "source_hash": rendered.source_hash,
                          "native_bytes": len(content), "renderer_version": rendered.renderer_version,
                          "configuration_hash": rendered.configuration_hash, "checks": rendered.checks,
                          "render_snapshot": rendered.snapshot.model_dump(mode="json"),
                          "functional_scope": "only the declared browser actions/assertions; appearance does not establish full correctness"}


def saved_version_reviews(session: Session, version_id: str) -> tuple[list, list]:
    originals = list(session.scalars(select(DesignReview).where(DesignReview.version_id == version_id).order_by(DesignReview.created_at, DesignReview.id)))
    version = session.get(DesignVersion, version_id)
    if version is None:
        return originals, []
    operations = list(session.scalars(select(DesignOperation).where(DesignOperation.session_id == version.session_id).order_by(DesignOperation.created_at, DesignOperation.id)))
    if any(not isinstance(row.payload, dict) for row in operations):
        raise VisualLimit("Saved review scope is unavailable")
    candidates = [row for row in operations if row.payload.get("kind") == "review_supplement"]
    if candidates:
        identities = {row.payload.get("version_id") for row in candidates if isinstance(row.payload.get("version_id"), str)}
        known = set(session.scalars(select(DesignVersion.id).where(DesignVersion.session_id == version.session_id, DesignVersion.id.in_(identities))))
        if any(not isinstance(row.payload.get("version_id"), str) or row.payload.get("version_id") not in known for row in candidates):
            raise VisualLimit("Saved review scope is unavailable")
    supplements = [row for row in candidates if row.payload.get("version_id") == version_id]
    return originals, supplements


def receipt_bound_reviews(version: DesignVersion, originals, supplements):
    """Use only each saved review's own matching receipt as a quorum vote."""
    saved = [(row.provider, row.session_id, row.critique, row.receipt) for row in originals]
    saved.extend((row.provider, row.session_id, row.payload.get("critique"), row.payload.get("receipt")) for row in supplements)
    critiques, receipts, unverified = [], [], []
    for provider, session_id, value, recorded in saved:
        try:
            critique = Critique.model_validate(value)
        except (TypeError, ValueError) as error:
            raise VisualLimit("A saved review is invalid; review coverage is unavailable") from error
        if (provider != critique.provider or session_id != version.session_id
                or critique.version_id != version.id or critique.canonical_hash != version.canonical_hash):
            unverified.append(critique)
            continue
        try:
            receipt = PixelReceipt(**{**recorded, "views": tuple(recorded["views"])})
            validate_critique(critique, receipt)
            if not 1 <= len(receipt.views) <= 4 or any(
                not isinstance(view, dict) or type(view.get("byte_size")) is not int
                or not 1 <= view["byte_size"] <= 64 * 1024 * 1024
                or not isinstance(view.get("sha256"), str) or len(view["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in view["sha256"])
                or not isinstance(view.get("transform"), str)
                for view in receipt.views
            ) or receipt.views[0]["sha256"] != version.canonical_hash:
                raise ValueError("Saved review overview differs from the version")
        except (KeyError, TypeError, ValueError):
            unverified.append(critique)
            continue
        critiques.append(critique)
        receipts.append(receipt)
    return critiques, receipts, unverified


def has_comparison_receipt(parent: DesignVersion | None, receipt):
    """Require this receipt to record the exact parent view used by the worker."""
    if parent is None:
        return False
    transform = f"comparison_parent_version_{parent.id}"
    return (len(receipt.views) >= 2 and isinstance(receipt.views[1], dict)
            and receipt.views[1].get("sha256") == parent.canonical_hash
            and receipt.views[1].get("transform") == transform)


def version_feedback_findings(session: Session, design: DesignSession, version: DesignVersion, *, _seen=()):
    """Carry unresolved ancestor obligations even when a critic omits new findings."""
    from .feedback import regression_summary, regression_targets
    if version.id in _seen or len(_seen) >= 24 or version.session_id != design.id:
        raise VisualLimit("Invalid or unbounded parent version chain")
    originals, supplements = saved_version_reviews(session, version.id)
    critiques, receipts, unverified = receipt_bound_reviews(version, originals, supplements)
    values = [row.critique for row in originals] + [row.payload.get("critique") for row in supplements]
    findings = [finding for value in values for finding in value.get("findings", [])]
    resolved_prior_ids = set()
    if version.parent_version_id:
        parent = session.get(DesignVersion, version.parent_version_id)
        if parent is None:
            raise VisualLimit("The prior design comparison is outside this immutable session")
        inherited = version_feedback_findings(session, design, parent, _seen=(*_seen, version.id))
        targets = regression_targets(design.frozen_brief, inherited)
        eligible = coverage(critiques, receipts, version_id=version.id, canonical_hash=version.canonical_hash,
                            generator_provider=version.generator_provider)["eligible_critic_providers"]
        voters = [critique for critique, receipt in zip(critiques, receipts, strict=True) if has_comparison_receipt(parent, receipt)]
        summary = regression_summary(targets, critiques, eligible, comparison_critiques=voters, unverified_critiques=unverified)
        resolved_prior_ids = {row["id"] for row in summary["dispositions"] if row["kind"] == "prior_finding"
                              and row["severity"] in {"material", "critical"} and row["status"] == "resolved"}
        known = {finding["id"] for finding in findings}
        for target in summary["dispositions"]:
            if target["kind"] == "prior_finding" and target["status"] != "resolved" and target["id"] not in known:
                findings.append({"id": target["id"], "description": target["description"], "severity": target["severity"], "category": "observed_defect"})
    # A model's single "addressed" label is not an independent resolution. Keep
    # raw saved critiques unchanged, and carry unsupported claims forward as
    # pending so the next version receives a regression target.
    unverified_ids = {finding.id for critique in unverified for finding in critique.findings}
    findings = [{**finding, "disposition": "pending"}
                if finding.get("severity") in {"material", "critical"} and finding.get("disposition") == "addressed"
                and (finding.get("id") in unverified_ids or finding.get("previous_finding_id") not in resolved_prior_ids)
                else finding for finding in findings]
    if len(findings) > 40:
        raise VisualLimit("Prior findings exceed the faithfully bounded regression context")
    return findings


def version_regression_targets(session: Session, design: DesignSession, version: DesignVersion):
    from .feedback import regression_targets
    if not version.parent_version_id:
        return []
    parent = session.get(DesignVersion, version.parent_version_id)
    if parent is None:
        raise VisualLimit("The prior design comparison is outside this immutable session")
    findings = version_feedback_findings(session, design, parent)
    return regression_targets(design.frozen_brief, findings)


def complete_version_critics(session: Session, design: DesignSession, version: DesignVersion) -> set[str]:
    originals, supplements = saved_version_reviews(session, version.id)
    targets = {row["id"] for row in version_regression_targets(session, design, version)}
    critiques, receipts, _ = receipt_bound_reviews(version, originals, supplements)
    parent = session.get(DesignVersion, version.parent_version_id) if version.parent_version_id else None
    covered = {}
    for critique, receipt in zip(critiques, receipts, strict=True):
        if targets and not has_comparison_receipt(parent, receipt):
            continue
        covered.setdefault(critique.provider, set()).update(check.target_id for check in critique.regression_checks)
    return {provider for provider, identities in covered.items() if targets <= identities}


def queue_version_reviews(session: Session, design: DesignSession, version: DesignVersion) -> list[DesignOperation]:
    """Persist independent critique intentions; worker funding precedes each send."""
    config = design.configuration
    critics = [provider for provider in config.get("critic_providers", []) if provider != version.generator_provider]
    if len(set(critics)) != 2 or not set(critics) <= set(design.recipients):
        raise VisualLimit("Two distinct configured critics outside the generator must share the frozen recipient scope")
    state = state_for(design)
    if state.status != "active" or design.status != "active":
        raise VisualLimit("Terminal or quarantined designs cannot schedule review")
    existing = list(session.scalars(select(DesignOperation).where(DesignOperation.session_id == design.id)))
    complete = complete_version_critics(session, design, version)
    rows = []
    for provider in critics:
        step = f"vision:{version.id}:{provider}"
        if provider in complete:
            continue
        if any(row.provider == provider and row.payload.get("step_id") == step and row.payload.get("status") in {"queued", "claimed", "authorized", "running"} for row in existing):
            continue
        if state.step_calls.get(step, 0) >= 2:
            raise VisualLimit("The original critic role has no remaining inference call")
        if any(row.provider == provider and row.payload.get("kind") == "review_supplement" and row.payload.get("version_id") == version.id for row in existing):
            raise VisualLimit("The bounded recovery review remains incomplete; retain this limitation")
        caps = config.get("capabilities", {}).get(provider, {})
        if caps.get("understands_images") is not True:
            raise VisualLimit("Configured critic cannot inspect actual images")
        request = {"kind": "vision", "version_id": version.id, "input_hash": version.canonical_hash,
                   "brief_hash": hashlib.sha256(json.dumps(design.frozen_brief, sort_keys=True).encode()).hexdigest(),
                   "configuration_hash": config.get("operator_configuration_hash"), "provider": provider}
        row = DesignOperation(id=new_id("designoperation"), session_id=design.id, provider=provider,
                              payload={**request, "status": "queued", "dispatch_status": "pending", "step_id": step,
                                       "target_version_id": version.id, "parent_version_id": None,
                                       "owner_id": design.owner_id, "session_id": design.id,
                                       "fence": design.execution_fence, "epoch": design.deployment_epoch,
                                       "bound_usd": config.get("vision_prices", {}).get(provider),
                                       "review_pool_tag": version.provenance.get("review_pool_tag", version.id),
                                       "queue_accepted_at": utcnow().isoformat(),
                                       "initial_admission_expires_at": design.preflight.get("initial_admission_expires_at") if design.preflight else None,
                                       "request_hash": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()})
        session.add(row)
        rows.append(row)
    return rows


def commit_version(session: Session, design: DesignSession, raster: Raster, directory: Path, *, expected_fence: int, parent_version_id: str | None = None, generator_provider: str | None = None, source_kind: str = "upload", operation_id: str | None = None, storage_reservation_id: str | None = None) -> DesignVersion:
    meeting = session.get(Meeting, design.meeting_id, populate_existing=True)
    if not meeting or meeting.deleted_at or design.status != "active" or design.execution_fence != expected_fence:
        raise VisualLimit("Deleted, cancelled or stale design cannot publish an artifact")
    latest = session.scalar(select(DesignVersion).where(DesignVersion.session_id == design.id).order_by(DesignVersion.version.desc()).limit(1))
    if parent_version_id != (latest.id if latest else None):
        raise VisualLimit("Edited candidate does not bind to the exact latest parent version")
    if source_kind == "upload" and latest is not None:
        raise VisualLimit("An upload establishes V1 only; later versions require an authorized edit")
    state = state_for(design)
    def valid_storage_reservation(reservation):
        if (not reservation or reservation.session_id != design.id or not isinstance(reservation.payload, dict)
                or reservation.payload.get("kind") != "asset_reservation" or reservation.payload.get("status") != "reserved"):
            return False
        if source_kind in {"generated", "edit"}:
            return bool(operation_id) and reservation.payload.get("external_operation_id") == operation_id
        return True

    if storage_reservation_id:
        reservation = session.scalar(select(DesignOperation).where(DesignOperation.id == storage_reservation_id).with_for_update().execution_options(populate_existing=True))
        if not valid_storage_reservation(reservation):
            raise VisualLimit("Original durable asset reservation is unavailable")
    check_storage_capacity(session, design, len(raster.canonical_bytes), exclude_reservation=storage_reservation_id)
    if source_kind not in {"upload", "generated", "edit", "native_diagram", "native_interface"}:
        raise VisualLimit("Unsupported artifact source")
    if source_kind in {"generated", "edit"}:
        operation = state.operations.get(operation_id)
        expected_kind = "generation" if source_kind == "generated" else "edit"
        if not operation or operation["kind"] != expected_kind or operation.get("published_version_id") or not generator_provider:
            raise VisualLimit("Generated content requires its original counted authorized operation and actual generator")
        if (source_kind == "generated") != (latest is None):
            raise VisualLimit("Generation establishes V1; a child version is an intentional edit")
    version_number = state.publish_version(parent_hash=latest.canonical_hash if latest else None, expected_parent_hash=latest.canonical_hash if latest else None)
    path = store_raster(directory / design.owner_id / design.id, raster)
    row = DesignVersion(id=new_id("designversion"), session_id=design.id, version=version_number,
                        parent_version_id=parent_version_id, canonical_hash=raster.canonical_hash,
                        original_hash=raster.original_hash, asset_path=str(path), width=raster.width, height=raster.height,
                        generator_provider=generator_provider, source_kind=source_kind,
                        provenance={"canonical_media": "image/png", "canonical_bytes": len(raster.canonical_bytes), "metadata_removed": True,
                                    "source_bytes_retained": False, "parent_hash": latest.canonical_hash if latest else None})
    session.add(row)
    if storage_reservation_id:
        reservation = session.get(DesignOperation, storage_reservation_id)
        if not valid_storage_reservation(reservation):
            raise VisualLimit("Original durable asset reservation is unavailable")
        reservation.payload = {**reservation.payload, "status": "settled", "actual_bytes": len(raster.canonical_bytes)}
    if operation_id:
        state.operations[operation_id]["published_version_id"] = row.id
    design.state, design.revision, design.updated_at = asdict(state), design.revision + 1, utcnow()
    # This operation is called only within a transaction whose design row and
    # meeting ownership/fence have already been locked. No model call occurs.
    session.flush()
    return row


def remove_unreferenced_assets(session: Session, design: DesignSession, artifact_directory: Path) -> None:
    """Clean a locked session's failed local publication, preserving saved assets."""
    versions = list(session.scalars(select(DesignVersion).where(DesignVersion.session_id == design.id)))
    retained = {Path(version.asset_path).resolve() for version in versions}
    retained.update(Path(version.provenance[key]).resolve() for version in versions for key in ("svg_path", "source_path") if version.provenance.get(key))
    namespace = artifact_directory / "visual" / design.owner_id / design.id
    reject_linked_path(namespace)
    if namespace.exists():
        for path in namespace.iterdir():
            if path.is_symlink() or not path.is_file():
                raise VisualLimit("Unexpected temporary visual storage entry")
            if path.resolve() not in retained:
                path.unlink()


def reap_asset_reservations(session: Session, artifact_directory: Path, *, now=None) -> int:
    """Reconcile abandoned bounded uploads/renders without renewing authority."""
    from datetime import timezone
    from uuid import UUID
    now = now or utcnow()
    reaped = 0
    candidates = list(session.scalars(select(DesignOperation).where(DesignOperation.provider == "local")))
    for operation in candidates:
        created = operation.created_at.replace(tzinfo=timezone.utc) if operation.created_at.tzinfo is None else operation.created_at
        if operation.payload.get("external_operation_id") or operation.payload.get("kind") != "asset_reservation" or operation.payload.get("status") != "reserved" or now - created < timedelta(minutes=10):
            continue
        probe = session.get(DesignSession, operation.session_id)
        if not probe:
            continue
        lock_policies(session, probe.owner_id)
        session.scalar(select(Meeting).where(Meeting.id == probe.meeting_id).with_for_update().execution_options(populate_existing=True))
        design = session.scalar(select(DesignSession).where(DesignSession.id == probe.id).with_for_update().execution_options(populate_existing=True))
        operation = session.get(DesignOperation, operation.id, populate_existing=True)
        if operation.payload.get("status") != "reserved":
            continue
        # No external image request is associated with a local byte hold.
        # A late publisher must recheck its original hold and is now fenced.
        operation.payload = {**operation.payload, "status": "released", "reason": "temporary_asset_expired"}
        remove_unreferenced_assets(session, design, artifact_directory)
        try:
            identity = str(UUID(operation.id.rsplit("_", 1)[1]))
        except ValueError:
            identity = None
        command = session.get(Command, (design.owner_id, identity)) if identity else None
        if command and not command.gone:
            command.result = {**design_view(session, design), "upload_status": "expired", "render_status": "expired"}
        reaped += 1
    return reaped


def commit_review(session: Session, design: DesignSession, version: DesignVersion, critique: Critique, receipt: PixelReceipt, *, expected_fence: int) -> DesignReview | DesignOperation:
    meeting = session.get(Meeting, design.meeting_id, populate_existing=True)
    if not meeting or meeting.deleted_at or design.status != "active" or design.execution_fence != expected_fence:
        raise VisualLimit("A fenced review cannot publish findings")
    if version.session_id != design.id or critique.provider not in design.recipients or receipt.version_id != version.id or receipt.canonical_hash != version.canonical_hash:
        raise VisualLimit("Review changed the version, frozen recipients or artifact scope")
    previous_ids = set()
    previous_findings = []
    if version.parent_version_id:
        parent = session.get(DesignVersion, version.parent_version_id)
        if parent is None:
            raise VisualLimit("The prior design comparison is outside this immutable session")
        previous_findings = version_feedback_findings(session, design, parent)
        previous_ids.update(finding["id"] for finding in previous_findings)
    validate_critique(critique, receipt, prior_finding_ids=previous_ids)
    from .feedback import regression_targets
    targets = regression_targets(design.frozen_brief, previous_findings) if version.parent_version_id else []
    if any(check.target_id not in {target["id"] for target in targets} for check in critique.regression_checks):
        raise VisualLimit("Regression check changed its server-owned target scope")
    # Model IDs are response-local aliases. Persist server-scoped finding IDs so
    # different critics/versions cannot collide or forge each other's findings.
    critique = critique.model_copy(update={"findings": [finding.model_copy(update={
        "id": "finding_" + hashlib.sha256(f"{version.id}:{critique.provider}:{receipt.id}:{finding.id}".encode()).hexdigest()[:40]
    }) for finding in critique.findings]})
    existing = session.scalar(select(DesignReview).where(DesignReview.version_id == version.id, DesignReview.provider == critique.provider))
    if existing:
        _, prior_supplements = saved_version_reviews(session, version.id)
        if critique.provider in complete_version_critics(session, design, version) or not targets or any(row.provider == critique.provider for row in prior_supplements):
            raise VisualLimit("A critic already has one complete committed review for this exact version")
        supplement = DesignOperation(id=new_id("designsupplement"), session_id=design.id, provider=critique.provider,
            payload={"kind": "review_supplement", "status": "completed", "version_id": version.id, "source_review_id": existing.id,
                     "receipt": asdict(receipt), "critique": critique.model_dump(mode="json")})
        session.add(supplement)
        design.revision += 1
        return supplement
    row = DesignReview(id=new_id("designreview"), session_id=design.id, version_id=version.id,
                       provider=critique.provider, receipt=asdict(receipt), critique=critique.model_dump(mode="json"))
    session.add(row)
    design.revision += 1
    return row


def _verified_version_view(session: Session, version: DesignVersion) -> dict:
    reviews, supplements = saved_version_reviews(session, version.id)
    critiques, receipts, unverified = receipt_bound_reviews(version, reviews, supplements)
    review_coverage = coverage(critiques, receipts, version_id=version.id, canonical_hash=version.canonical_hash, generator_provider=version.generator_provider)
    from .feedback import regression_summary, regression_targets
    design = session.get(DesignSession, version.session_id)
    targets = version_regression_targets(session, design, version)
    parent = session.get(DesignVersion, version.parent_version_id) if version.parent_version_id else None
    voters = [critique for critique, receipt in zip(critiques, receipts, strict=True) if has_comparison_receipt(parent, receipt)]
    regression = regression_summary(targets, critiques, review_coverage["eligible_critic_providers"], comparison_critiques=voters, unverified_critiques=unverified)
    if unverified:
        review_coverage["limitations"].append("Some saved reviews lack matching evidence and cannot count toward coverage or resolution.")
    if targets and any(critique.regression_checks and not has_comparison_receipt(parent, receipt)
                       for critique, receipt in zip(critiques, receipts, strict=True)):
        review_coverage["limitations"].append("Some saved comparisons lack the required parent view and cannot resolve earlier findings.")
    if max(version.width, version.height) > MAX_DIMENSION:
        review_coverage["limitations"].append("This historical image exceeds the current 8192-pixel dimension limit. Resize it and create a new visual brief before requesting further review.")
    return {"id": version.id, "version": version.version, "parent_version_id": version.parent_version_id,
            "canonical_hash": version.canonical_hash, "original_hash": version.original_hash,
            "width": version.width, "height": version.height, "generator_provider": version.generator_provider,
            "source_kind": version.source_kind, "provenance": {key: value for key, value in version.provenance.items() if key not in {"svg_path", "source_path"}},
            "image_url": f"/api/design-versions/{version.id}/image", "reviews": [row.critique for row in reviews], "review_supplements": [row.payload["critique"] for row in supplements],
            "coverage": review_coverage, "regression_review": regression}


def _unavailable_version_view(session: Session, version: DesignVersion) -> dict:
    """Return a conservative view when saved review data cannot be verified."""
    originals = list(session.scalars(select(DesignReview).where(DesignReview.version_id == version.id).order_by(DesignReview.created_at, DesignReview.id)))
    operations = list(session.scalars(select(DesignOperation).where(DesignOperation.session_id == version.session_id).order_by(DesignOperation.created_at, DesignOperation.id)))
    supplements = [row for row in operations if isinstance(row.payload, dict) and row.payload.get("kind") == "review_supplement" and row.payload.get("version_id") == version.id]
    raw_reviews = [row.critique for row in originals]
    raw_supplements = [row.payload.get("critique") for row in supplements]
    def typed_reviews(values):
        result = []
        for value in values:
            try:
                result.append(Critique.model_validate(value).model_dump(mode="json"))
            except (TypeError, ValueError):
                continue
        return result[:3]
    # Retain any individually valid severe finding identities as unresolved,
    # while keeping malformed original records unchanged in storage.
    known_versions = set(session.scalars(select(DesignVersion.id).where(DesignVersion.session_id == version.session_id)))
    unscoped = [row.payload.get("critique") for row in operations if isinstance(row.payload, dict)
                and row.payload.get("kind") == "review_supplement"
                and (not isinstance(row.payload.get("version_id"), str) or row.payload.get("version_id") not in known_versions)]
    raw_history = [*raw_reviews, *raw_supplements, *unscoped]
    current, seen = version, {version.id}
    while current.parent_version_id and len(seen) < 24:
        parent = session.get(DesignVersion, current.parent_version_id)
        if parent is None or parent.id in seen or parent.session_id != version.session_id:
            break
        seen.add(parent.id)
        raw_history.extend(row.critique for row in session.scalars(select(DesignReview).where(DesignReview.version_id == parent.id)))
        raw_history.extend(row.payload.get("critique") for row in operations if isinstance(row.payload, dict)
                           and row.payload.get("kind") == "review_supplement" and row.payload.get("version_id") == parent.id)
        current = parent
    blockers = set()
    for value in raw_history:
        if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
            continue
        for item in value["findings"][:120]:
            try:
                finding = Finding.model_validate(item)
            except (TypeError, ValueError):
                continue
            if finding.severity in {"material", "critical"}:
                blockers.add(finding.id)
    return {"id": version.id, "version": version.version, "parent_version_id": version.parent_version_id,
            "canonical_hash": version.canonical_hash, "original_hash": version.original_hash,
            "width": version.width, "height": version.height, "generator_provider": version.generator_provider,
            "source_kind": version.source_kind, "provenance": {key: value for key, value in version.provenance.items() if key not in {"svg_path", "source_path"}},
            "image_url": f"/api/design-versions/{version.id}/image", "reviews": typed_reviews(raw_reviews),
            "review_supplements": typed_reviews(raw_supplements),
            "coverage": {"eligible_critic_providers": [], "complete": False,
                         "generator_provenance": "unknown" if version.generator_provider is None else "known",
                         "limitations": ["Saved review information is unavailable; coverage and comparison results cannot be verified."]},
            "regression_review": {"complete": False, "pending_target_ids": [], "unresolved_blockers": sorted(blockers)[:120], "dispositions": []}}


def version_view(session: Session, version: DesignVersion) -> dict:
    try:
        return _verified_version_view(session, version)
    except (VisualLimit, ValueError, TypeError, KeyError, AttributeError):
        return _unavailable_version_view(session, version)


def design_view(session: Session, design: DesignSession) -> dict:
    versions = list(session.scalars(select(DesignVersion).where(DesignVersion.session_id == design.id).order_by(DesignVersion.version)))
    operations = list(session.scalars(select(DesignOperation).where(DesignOperation.session_id == design.id).order_by(DesignOperation.created_at, DesignOperation.id)))
    public_statuses = {"queued", "claimed", "authorized", "running", "failed", "unknown", "refused", "blocked", "fenced"}
    public_kinds = {"generation", "edit", "vision"}
    invalid_operations = any(not isinstance(operation.payload, dict)
                             or (not isinstance(operation.payload.get("status"), (str, type(None))))
                             or (isinstance(operation.payload.get("status"), str) and operation.payload["status"] in public_statuses
                                 and (not isinstance(operation.payload.get("kind"), str) or operation.payload["kind"] not in public_kinds))
                             for operation in operations)
    operations = [operation for operation in operations if isinstance(operation.payload, dict)
                  and isinstance(operation.payload.get("kind"), str) and operation.payload["kind"] in public_kinds
                  and isinstance(operation.payload.get("status"), str)]
    failures = [{"id": operation.id, "kind": operation.payload.get("kind"),
                 "error": operation.payload.get("error_code") if isinstance(operation.payload.get("error_code"), str)
                 and 0 < len(operation.payload["error_code"]) <= 500 else "operation_unavailable"}
                for operation in operations if operation.payload.get("status") in {"failed", "unknown", "refused", "blocked", "fenced"}]
    return {"id": design.id, "meeting_id": design.meeting_id, "mode": design.mode, "status": design.status,
            "revision": design.revision, "brief": design.frozen_brief, "recipients": design.recipients,
            "selected_version_id": design.selected_version_id, "selection_recorded": design.selection_recorded,
            "versions": [version_view(session, version) for version in versions], "state": design.state,
            "pending_operations": [{"id": operation.id, "kind": operation.payload.get("kind"), "status": operation.payload.get("status")} for operation in operations if operation.payload.get("status") in {"queued", "claimed", "authorized", "running"}],
            # Admission can fail before consuming one of the 24 calls. Keep the
            # response bounded even after repeated blocked commands; immutable
            # older command receipts remain available for reconciliation.
            "operation_failures": failures[-24:],
            "paid_access_enabled": bool(design.configuration.get("funded")), "limitations": ["Visual demand and human quality gates remain pending.",
                "Review remains incomplete until two actual eligible critics inspect this exact version."] + (["Some saved operation details are unavailable."] if invalid_operations else [])
                + (["Showing the most recent 24 operation failures; older command receipts remain saved."] if len(failures) > 24 else [])}


def purge_designs(session: Session, meeting_id: str, artifact_directory: Path) -> None:
    """Erase content while retaining noncontent counters/unknown financial IDs."""
    from round_robin.operations.artifact_lifecycle import hold_artifact_lifecycle
    lock_policies(session, "owner_operator")
    hold_artifact_lifecycle(session, artifact_directory)
    for design in session.scalars(select(DesignSession).where(DesignSession.meeting_id == meeting_id)):
        versions = list(session.scalars(select(DesignVersion).where(DesignVersion.session_id == design.id)))
        namespace = artifact_directory / "visual" / design.owner_id / design.id
        reject_linked_path(namespace)
        allowed = namespace.resolve()
        paths_to_delete = []
        for version in versions:
            paths = [version.asset_path] + [version.provenance.get(key) for key in ("svg_path", "source_path") if version.provenance.get(key)]
            for value in paths:
                path = Path(value)
                reject_linked_path(path)
                path = path.resolve()
                if not path.is_relative_to(allowed):
                    raise VisualLimit("Refuse to purge a visual path outside its owned private storage")
                paths_to_delete.append(path)
        for path in paths_to_delete:
            path.unlink(missing_ok=True)
        # Failed DB publication can leave an orphan after a durable asset hold.
        # Purge only this immutable owner/session namespace, including orphans.
        if namespace.exists():
            for child in namespace.iterdir():
                if child.is_symlink() or not child.is_file():
                    raise VisualLimit("Unexpected visual storage entry requires operator recovery")
                child.unlink(missing_ok=True)
        session.execute(delete(DesignReview).where(DesignReview.session_id == design.id))
        session.execute(delete(DesignVersion).where(DesignVersion.session_id == design.id))
        state = state_for(design)
        state.status, state.fence = "deleted", state.fence + 1
        for operation in state.operations.values():
            if operation["status"] == "authorized":
                operation["status"] = "unknown"
        design.status, design.execution_fence, design.state = "deleted", state.fence, asdict(state)
        design.frozen_brief, design.preflight, design.selected_version_id = {}, None, None
        release_unused_visual_finish(session, design, "meeting_deleted")
        for operation in session.scalars(select(DesignOperation).where(DesignOperation.session_id == design.id)):
            operation.payload = {key: value for key, value in operation.payload.items() if key in {"operation_id", "provider_job_id", "request_hash", "status", "bound_usd", "actual_usd", "fence", "epoch", "unknown_outcome"}}


def quarantine_restored_designs(session: Session, deployment_epoch: int) -> None:
    for design in session.scalars(select(DesignSession).where(DesignSession.status == "active")):
        state = state_for(design)
        state.status, state.fence, state.epoch = "quarantined", state.fence + 1, deployment_epoch
        for operation in state.operations.values():
            if operation["status"] == "authorized":
                operation["status"] = "unknown"
        design.status, design.execution_fence, design.deployment_epoch = "quarantined", state.fence, deployment_epoch
        design.state, design.preflight = asdict(state), None
        release_unused_visual_finish(session, design, "restore_quarantine")


def release_unused_visual_finish(session: Session, design: DesignSession, reason: str) -> None:
    lock_policies(session, design.owner_id)
    from .finish import valid_visual_finish_pool
    for operation in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == design.id, BudgetOperation.owner_id == design.owner_id, BudgetOperation.kind == "visual_finish_hold", BudgetOperation.status == "held").with_for_update().execution_options(populate_existing=True)):
        if not valid_visual_finish_pool(operation, design):
            continue
        operation.status, operation.reserved_usd = "released", "0"
        operation.metadata_json = {**operation.metadata_json, "release_reason": reason}


def _private_copy(source: Path, target: Path, expected_hash: str, maximum_bytes: int) -> None:
    reject_linked_path(source)
    reject_linked_path(target)
    content = checked_private_bytes(source, expected_hash, maximum_bytes)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        checked_private_bytes(target, expected_hash, maximum_bytes)
        return
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    parent = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def backup_visual_assets(tables: dict, artifact_directory: Path, backup_directory: Path) -> list[dict]:
    """Copy referenced owned pixels/native sources into the private backup."""
    if artifact_directory.is_symlink():
        raise VisualLimit("Visual artifact root cannot be a symlink")
    root = artifact_directory.resolve()
    items, seen = [], set()
    for row in tables.get("design_versions", []):
        provenance = row.get("provenance", {})
        sources = [(row["asset_path"], row["canonical_hash"])]
        sources.extend((provenance[key], provenance.get("svg_hash" if key == "svg_path" else "source_hash")) for key in ("svg_path", "source_path") if provenance.get(key))
        for original, expected in sources:
            source = Path(original)
            if source.is_symlink() or not source.resolve().is_relative_to(root / "visual"):
                raise VisualLimit("Visual backup refuses paths outside owned private storage")
            reject_linked_path(source)
            relative = source.resolve().relative_to(root)
            if str(source) in seen:
                continue
            if source.suffix not in {".png", ".svg", ".json", ".html"} or not expected:
                raise VisualLimit("Visual backup requires a typed hash-bound artifact")
            key = f"visual_assets/{expected}{source.suffix}"
            maximum = 64 * 1024 * 1024 if source.suffix == ".png" else 1024 * 1024
            _private_copy(source, backup_directory / key, expected, maximum)
            items.append({"key": key, "sha256": expected, "original_path": str(source), "relative_path": relative.as_posix(), "maximum_bytes": maximum})
            seen.add(str(source))
    return items


def restore_visual_assets(backup_root: Path, target_artifacts: Path, items: list, tables: dict) -> None:
    """Restore only hash-checked private assets and rebase table references."""
    if len(items) > 2000 or target_artifacts.is_symlink():
        raise VisualLimit("Visual restore resource/path bound exceeded")
    mapping = {}
    backup_base = backup_root.resolve()
    target_base = target_artifacts.resolve()
    for item in items:
        key, relative = Path(item["key"]), Path(item["relative_path"])
        if key.is_absolute() or relative.is_absolute() or ".." in key.parts or ".." in relative.parts or not key.parts or key.parts[0] != "visual_assets" or not relative.parts or relative.parts[0] != "visual":
            raise VisualLimit("Visual restore contains an unsafe archive/target path")
        if relative.suffix not in {".png", ".svg", ".json", ".html"} or key.name != f"{item['sha256']}{relative.suffix}":
            raise VisualLimit("Visual restore contains an unsupported artifact type or identity")
        source, target = backup_base / key, target_base / relative
        if source.is_symlink() or not source.resolve().is_relative_to(backup_base) or not target.resolve().is_relative_to(target_base / "visual"):
            raise VisualLimit("Visual restore refuses linked or foreign paths")
        maximum = 64 * 1024 * 1024 if target.suffix == ".png" else 1024 * 1024
        _private_copy(source, target, item["sha256"], maximum)
        mapping[item["original_path"]] = str(target)
    for row in tables.get("design_versions", []):
        if row["asset_path"] not in mapping:
            raise VisualLimit("Visual snapshot references a missing canonical asset")
        row["asset_path"] = mapping[row["asset_path"]]
        provenance = row.get("provenance", {})
        for key in ("svg_path", "source_path"):
            if provenance.get(key):
                if provenance[key] not in mapping:
                    raise VisualLimit("Visual snapshot references missing native source")
                provenance[key] = mapping[provenance[key]]
