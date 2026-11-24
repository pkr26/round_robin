"""One immutable authorized image/vision operation per isolated worker task."""
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
import os
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from celery import Celery
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from round_robin.configuration import settings
from round_robin.database.models import Attempt, BudgetOperation, Meeting, PolicyBucket, utcnow
from round_robin.database.services import lock_policies, new_id
from round_robin.database.session import SessionLocal
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.orchestration.budget import BudgetDenied, _exact_money, _policy_checks, amount, aware, reconcile_operation
from round_robin.orchestration.admission import attempt_capacity_provider, attempt_occupies_capacity
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import ImageAuthorization, ImageFundingLocked, configured_image_provider, verify_image_configuration
from round_robin.schemas.common import money
from round_robin.visual.assets import validate_canonical_raster
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.finish import valid_visual_finish_pool
from round_robin.visual.jobs import request_hash
from round_robin.visual.reviews import PixelReceipt, PixelView, independent_review_packet, issue_review_receipt
from round_robin.visual.schemas import Critique, VisualBrief
from round_robin.visual.service import commit_review, commit_version, owned_asset_bytes, queue_version_reviews, remove_unreferenced_assets, reserve_asset_storage

IMAGE_BROKER = os.environ.get("ROUND_ROBIN_IMAGE_BROKER_URL", "redis://127.0.0.1:56379/1")
CORE_BROKER = os.environ.get("ROUND_ROBIN_BROKER_URL", "redis://127.0.0.1:56379/0")
def _broker_database(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("Image workers require an explicit isolated Redis database")
    return int(parsed.path.strip("/") or "0")


if _broker_database(IMAGE_BROKER) == _broker_database(CORE_BROKER):
    raise ValueError("Image and ordinary workers require separate broker namespaces/visibility policies")

image_app = Celery("round_robin_images", broker=IMAGE_BROKER)
image_app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
                      task_soft_time_limit=630, task_time_limit=660, worker_concurrency=1,
                      broker_transport_options={"visibility_timeout": 900}, result_backend_transport_options={"visibility_timeout": 900},
                      visibility_timeout=900, task_default_queue=os.environ.get("ROUND_ROBIN_IMAGE_TASK_QUEUE", "round_robin_images"))
IMAGE_LEASE_SECONDS = 720
IMAGE_HEARTBEAT_SECONDS = 10


def _readable_operation_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str):
        return False
    status = payload.get("status")
    if not isinstance(status, str) and not (payload["kind"] == "review_supplement" and status is None):
        return False
    dispatch = payload.get("dispatch_status")
    return dispatch is None or isinstance(dispatch, str)


def image_request_timeout(remaining_active_seconds: float, elapsed_task_seconds: float, *, protected_review_seconds: float = 190) -> float:
    timeout = min(600.0, remaining_active_seconds - protected_review_seconds - 25, 630 - elapsed_task_seconds - 25)
    if timeout <= 0:
        raise ValueError("Image execution has no remaining bounded persistence/cleanup allowance")
    return timeout


def _locked_design(session, operation_id):
    snapshot = session.get(DesignOperation, operation_id)
    if snapshot is None:
        return None, None, None
    scope = session.get(DesignSession, snapshot.session_id)
    if scope is None:
        return None, None, None
    deployment, _ = lock_policies(session, scope.owner_id)
    meeting = session.scalar(select(Meeting).where(Meeting.id == scope.meeting_id).with_for_update().execution_options(populate_existing=True))
    design = session.scalar(select(DesignSession).where(DesignSession.id == scope.id).with_for_update().execution_options(populate_existing=True))
    operation = session.scalar(select(DesignOperation).where(DesignOperation.id == operation_id).with_for_update().execution_options(populate_existing=True))
    return design, operation, (meeting, deployment)


def _authority(design, operation, scope, *, artifact_directory=None, session=None):
    from round_robin.operations.durability import marker_pending
    if marker_pending(artifact_directory, session=session):
        raise VisualLimit("Independent storage recovery is required")
    meeting, deployment = scope
    payload = operation.payload
    if payload.get("kind") not in {"vision", "generation", "edit"}:
        raise VisualLimit("Unsupported visual inference operation")
    if operation.provider not in design.recipients:
        raise VisualLimit("Visual operation adds an unapproved provider recipient")
    if payload["kind"] in {"generation", "edit"} and (design.mode != "improve" or operation.provider != design.configuration.get("generator_provider")):
        raise VisualLimit("Generation/editing differs from the frozen mode or configured generator")
    if not meeting or meeting.deleted_at or design.status != "active" or design.execution_fence != payload["fence"] or design.deployment_epoch != payload["epoch"] or deployment.metadata_json.get("deployment_epoch", design.deployment_epoch) != design.deployment_epoch:
        raise VisualLimit("Visual operation authority is deleted, stale or restored")
    if deployment.metadata_json.get("dispatch_locked") or deployment.metadata_json.get("restore_quarantine"):
        raise VisualLimit("Visual dispatch is paused")
    if (payload.get("owner_id"), payload.get("session_id")) != (design.owner_id, design.id):
        raise VisualLimit("Visual operation belongs to a different owner/session")
    verify_image_configuration(design.configuration)


def _proxy(design):
    config = design.configuration
    return SimpleNamespace(id=design.id, owner_id=design.owner_id, deployment_epoch=design.deployment_epoch,
        configuration={**config, "limits": {"per_run_usd": str(min(amount(design.maximum_usd), amount(config["maximum_usd"]))), "owner_daily_usd": config["owner_daily_usd"], "owner_monthly_usd": config["owner_monthly_usd"], "deployment_daily_usd": config["deployment_daily_usd"], "deployment_monthly_usd": config["deployment_monthly_usd"]}})


def _capacity(session, design, operation):
    active_text = [attempt for attempt in session.scalars(select(Attempt).where(Attempt.status == "authorized", Attempt.completed_at.is_(None))) if attempt_occupies_capacity(session, attempt)]
    if any(attempt_capacity_provider(session, attempt) is None for attempt in active_text):
        return False
    active_images = []
    for current, owner in session.execute(select(DesignOperation, DesignSession).outerjoin(DesignSession, DesignSession.id == DesignOperation.session_id)):
        data = current.payload
        if owner is None:
            raise VisualLimit("Saved visual operation scope is unavailable")
        if not _readable_operation_payload(data):
            # Purge strips the kind, but a fenced queued/terminal tombstone cannot
            # dispatch. Authorized or unknown tombstones remain ambiguous.
            if owner.status == "deleted" and isinstance(data, dict) and isinstance(data.get("status"), str):
                if data.get("status") in {"queued", "blocked", "completed", "failed", "fenced", "reserved", "released", "settled"}:
                    continue
                if data.get("status") in {"authorized", "unknown"}:
                    # Purge removes the lease and kind, so it cannot establish
                    # that an already authorized request has stopped running.
                    active_images.append(current)
                    continue
            raise VisualLimit("Saved visual operation metadata is unavailable")
        expiry = data.get("lease_expires_at")
        if current.id != operation.id and data.get("status") == "authorized" and expiry and aware(datetime.fromisoformat(expiry)) > utcnow():
            active_images.append(current)
    if len(active_text) + len(active_images) >= 3 or sum(item.provider == operation.provider for item in active_text + active_images) >= 2:
        return False
    if operation.payload["kind"] in {"generation", "edit"} and any(item.payload.get("kind") not in ("vision",) for item in active_images):
        return False
    return True


@_exact_money
def _reserve_operation(session, design, operation, state, *, request_seconds=0):
    """Mutate the sole review pool and authorize one operation in the same TX."""
    config, payload = design.configuration, operation.payload
    kind, provider = payload["kind"], operation.provider
    expected_bound = config["vision_prices"][provider] if kind == "vision" else config["generation_price_usd" if kind == "generation" else "edit_price_usd"]
    if amount(payload.get("bound_usd", expected_bound)) != amount(expected_bound):
        raise VisualLimit("Frozen operation price differs from configured conservative pricing")
    if session.get(BudgetOperation, operation.id):
        raise VisualLimit("An immutable visual authorization cannot be sent again")
    proxy = _proxy(design)
    tag = payload.get("review_pool_tag") or (payload.get("target_version_id") if kind == "vision" else payload["step_id"])
    pool_id = f"visualfinish_{design.id}"
    pool = _checked_finish_pool(session, design)
    if pool is None:
        pool = BudgetOperation(id=pool_id, run_id=design.id, owner_id=design.owner_id, kind="visual_finish_hold", status="held", reserved_usd="0", settled_usd=None, authorized_at=utcnow(), metadata_json={"target_tag": None, "roles_remaining": []})
        session.add(pool)
        session.flush()
    if pool.metadata_json.get("target_tag") != tag:
        if pool.metadata_json.get("roles_remaining"):
            raise VisualLimit("Finish the current candidate's required reviews before another image")
        required = sum((amount(config["vision_prices"][critic]) for critic in config["critic_providers"]), Decimal("0"))
        _policy_checks(session, proxy, required)
        pool.reserved_usd = str(required)
        pool.status = "held"
        pool.metadata_json = {"target_tag": tag, "roles_remaining": list(config["critic_providers"]), "pool_version": pool.metadata_json.get("pool_version", 0) + 1}
    roles = list(pool.metadata_json["roles_remaining"])
    transfer = kind == "vision" and provider in roles
    bound = amount(expected_bound)
    next_roles = [critic for critic in roles if not transfer or critic != provider]
    remaining_hold = amount(pool.reserved_usd) - bound if transfer else amount(pool.reserved_usd)
    if remaining_hold < 0:
        raise VisualLimit("The required review pool is underfunded")
    _policy_checks(session, proxy, Decimal("0") if transfer else bound)
    state.authorize(operation.id, payload["step_id"], kind, str(bound), now=utcnow().timestamp(), expected_fence=design.execution_fence, epoch=design.deployment_epoch,
        review_money=str(remaining_hold), review_calls=len(next_roles), review_seconds=95 * len(next_roles), request_seconds=request_seconds)
    if transfer:
        pool.reserved_usd = str(remaining_hold)
        pool.metadata_json = {**pool.metadata_json, "roles_remaining": next_roles}
    ledger = BudgetOperation(id=operation.id, run_id=design.id, owner_id=design.owner_id, kind=f"visual_{kind}", status="reserved", reserved_usd=str(bound), settled_usd=None, authorized_at=utcnow(), metadata_json={"model_call": True, "step_id": payload["step_id"], "provider": provider, "session_id": design.id, "epoch": design.deployment_epoch, "fence": design.execution_fence})
    session.add(ledger)
    return ImageAuthorization(operation.id, design.owner_id, design.id, str(bound), design.execution_fence, design.deployment_epoch, payload["step_id"])


@_exact_money
def _checked_finish_pool(session, design):
    pool = session.scalar(select(BudgetOperation).where(BudgetOperation.id == f"visualfinish_{design.id}").with_for_update().execution_options(populate_existing=True))
    if pool is not None and (not valid_visual_finish_pool(pool, design) or (design.status == "active" and pool.status != "held")):
        raise VisualLimit("Saved visual finish pool is unavailable")
    if pool is not None and design.status == "active":
        if not isinstance(design.configuration, dict):
            raise VisualLimit("Saved visual finish pool is unavailable")
        roles = pool.metadata_json.get("roles_remaining", [])
        critics = design.configuration.get("critic_providers")
        prices = design.configuration.get("vision_prices")
        if (not isinstance(critics, list) or len(critics) != 2
                or any(not isinstance(critic, str) for critic in critics)
                or len(set(critics)) != len(critics) or not isinstance(prices, dict)
                or any(role not in critics or role not in prices for role in roles)):
            raise VisualLimit("Saved visual finish pool is unavailable")
        try:
            role_prices = [amount(money(prices[role])) for role in roles]
        except (ValueError, BudgetDenied):
            raise VisualLimit("Saved visual finish pool is unavailable") from None
        if any(price <= 0 for price in role_prices):
            raise VisualLimit("Saved visual finish pool is unavailable")
        reserved = amount(pool.reserved_usd)
        expected = sum(role_prices, Decimal("0"))
        if reserved < expected:
            raise VisualLimit("Saved visual finish pool is underfunded")
        if reserved > expected:
            raise VisualLimit("Saved visual finish pool is unavailable")
    return pool


def _release_unused_pool(session, design):
    if design.status == "active":
        return
    try:
        pool = _checked_finish_pool(session, design)
    except VisualLimit:
        return
    if pool and pool.status == "held":
        pool.reserved_usd, pool.status = "0", "released"


def _close_unserviceable_design(session, design):
    """Retire impossible future work without clearing sent/unknown exposure."""
    if design.status != "active":
        _release_unused_pool(session, design)
        return False
    state = VisualState(**design.state)
    operations = list(session.scalars(select(DesignOperation).where(DesignOperation.session_id == design.id)))
    if any(not _readable_operation_payload(item.payload) for item in operations):
        raise VisualLimit("Saved visual operation metadata is unavailable")
    if any(item.payload.get("kind") == "review_supplement" for item in operations):
        from round_robin.visual.service import saved_version_reviews
        version_id = session.scalar(select(DesignVersion.id).where(DesignVersion.session_id == design.id).limit(1))
        if version_id is None:
            raise VisualLimit("Saved review scope is unavailable")
        saved_version_reviews(session, version_id)
    queued = [item for item in operations if item.payload.get("status") == "queued" and item.payload.get("kind") in {"vision", "generation", "edit"}]
    status = reason = None
    now = utcnow()
    if state.paid_admitted and now.timestamp() - state.admitted_at >= 1800:
        status, reason = "expired", "visual_active_deadline_exceeded"
    elif not state.paid_admitted and queued:
        for item in queued:
            expiry = item.payload.get("initial_admission_expires_at") or (design.preflight or {}).get("initial_admission_expires_at")
            if not expiry or aware(datetime.fromisoformat(expiry)) <= now:
                status, reason = "expired", "visual_admission_expired"
                break
    if status is None and (state.paid_admitted or queued):
        try:
            verify_image_configuration(design.configuration)
        except ImageFundingLocked:
            status, reason = "quarantined", "visual_configuration_unavailable"
    if status is None:
        return False
    state.status, state.fence = status, state.fence + 1
    design.status, design.execution_fence, design.state = status, state.fence, asdict(state)
    design.revision += 1
    design.preflight = None
    for item in queued:
        item.payload = {**item.payload, "status": "blocked", "dispatch_status": "done", "error_code": reason}
    _release_unused_pool(session, design)
    return True


def _request_allowance(session, design, operation):
    payload = operation.payload
    tag = payload.get("review_pool_tag") or (payload.get("target_version_id") if payload["kind"] == "vision" else payload["step_id"])
    pool = _checked_finish_pool(session, design)
    roles = list(pool.metadata_json.get("roles_remaining", [])) if pool and pool.metadata_json.get("target_tag") == tag else list(design.configuration["critic_providers"])
    if payload["kind"] == "vision":
        roles = [provider for provider in roles if provider != operation.provider]
    remaining = 1800 - (utcnow().timestamp() - design.state["admitted_at"])
    # Leave a small transaction allowance between constructing the packet and
    # the locked admission check; decoding/preflight is completed before this.
    timeout = image_request_timeout(remaining - 1, 0, protected_review_seconds=95 * len(roles))
    return min(90, timeout) if payload["kind"] == "vision" else timeout


def _packet(session, design, operation, provider, artifact_directory):
    payload = operation.payload
    result = {"kind": payload["kind"], "brief": design.frozen_brief, "owner_id": design.owner_id, "session_id": design.id, "step_id": payload["step_id"], "timeout_seconds": _request_allowance(session, design, operation)}
    if payload["kind"] in {"generation", "edit"}:
        result["edit_intent"] = payload.get("edit_intent", [])
    if payload["kind"] in {"vision", "edit"}:
        version = session.get(DesignVersion, payload.get("target_version_id") or payload.get("parent_version_id"))
        if not version or version.session_id != design.id or version.canonical_hash != payload.get("input_hash", version.canonical_hash):
            raise VisualLimit("Image operation changed its immutable parent/version hash")
        pixels = owned_asset_bytes(version, design, artifact_directory)
        if payload["kind"] == "vision":
            if operation.provider not in design.configuration["critic_providers"] or operation.provider == version.generator_provider:
                raise VisualLimit("An actual generator cannot satisfy independent critic coverage")
            views = [PixelView(pixels, version.canonical_hash)]
            prior_context = None
            if version.parent_version_id:
                parent = session.get(DesignVersion, version.parent_version_id)
                if not parent or parent.session_id != design.id:
                    raise VisualLimit("The prior design comparison is outside this immutable session")
                previous_pixels = owned_asset_bytes(parent, design, artifact_directory)
                views.append(PixelView(previous_pixels, parent.canonical_hash, f"comparison_parent_version_{parent.id}"))
                from round_robin.visual.service import version_feedback_findings
                prior_findings = version_feedback_findings(session, design, parent)
                prior_context = {"parent_version_id": parent.id, "parent_hash": parent.canonical_hash, "edit_intent": version.provenance.get("edit_intent", []), "findings": prior_findings}
                from round_robin.visual.feedback import regression_targets, verify_edit_bundle
                prior_context["regression_targets"] = regression_targets(design.frozen_brief, prior_findings)
                if version.provenance.get("edit_bundle") is not None:
                    prior_context["edit_bundle"] = verify_edit_bundle(version.provenance["edit_bundle"], version.provenance.get("edit_bundle_hash"),
                        brief=design.frozen_brief, parent_version_id=parent.id, parent_hash=parent.canonical_hash)
                    prior_context["edit_bundle_hash"] = version.provenance["edit_bundle_hash"]
            receipt = issue_review_receipt(provider.capabilities, version.id, version.canonical_hash, views)
            result.update(independent_review_packet(VisualBrief.model_validate(design.frozen_brief), receipt, views, prior_context=prior_context))
        else:
            result["images"] = [pixels]
            from round_robin.visual.feedback import build_edit_bundle, verify_edit_bundle
            from round_robin.visual.service import version_feedback_findings
            findings = version_feedback_findings(session, design, version)
            # Old queued operations did not contain bundles. Resolve their saved
            # choices from immutable parent reviews before the authorization
            # transaction; the resulting request hash binds the exact content.
            bundle, bundle_hash = build_edit_bundle(brief=design.frozen_brief, parent_version_id=version.id,
                parent_hash=version.canonical_hash, findings=findings,
                dispositions=payload.get("finding_dispositions", {}), intent=payload.get("edit_intent", ""))
            if payload.get("edit_bundle") is not None:
                verify_edit_bundle(payload["edit_bundle"], payload.get("edit_bundle_hash"), brief=design.frozen_brief,
                    parent_version_id=version.id, parent_hash=version.canonical_hash)
                if payload["edit_bundle"] != bundle:
                    raise VisualLimit("Saved edit choices differ from the immutable parent findings")
            operation.payload = {**operation.payload, "edit_bundle": bundle, "edit_bundle_hash": bundle_hash}
            result.update(edit_bundle=bundle, edit_bundle_hash=bundle_hash)
    if len(json.dumps({key: value for key, value in result.items() if key != "images"}, ensure_ascii=False, allow_nan=False).encode()) > 128 * 1024:
        raise VisualLimit("Visual packet metadata exceeds the bounded context")
    return result


def _heartbeat(operation_id, token, session_factory, stop):
    while not stop.wait(IMAGE_HEARTBEAT_SECONDS):
        try:
            with session_factory() as session, session.begin():
                design, operation, scope = _locked_design(session, operation_id)
                if not design or operation.payload.get("lease_owner") != token or operation.payload.get("status") != "authorized":
                    return
                _authority(design, operation, scope, session=session)
                operation.payload = {**operation.payload, "lease_expires_at": (utcnow() + timedelta(seconds=IMAGE_LEASE_SECONDS)).isoformat()}
        except (SQLAlchemyError, VisualLimit, ImageFundingLocked):
            return


def execute_image_operation(operation_id, *, session_factory=SessionLocal, provider_registry=None, artifact_directory=None):
    artifact_directory = Path(artifact_directory or settings.artifact_directory)
    token = new_id("imagelease")
    try:
        with session_factory() as session, session.begin():
            design, operation, scope = _locked_design(session, operation_id)
            if not design:
                return {"status": "already_authorized_or_settled"}
            if not _readable_operation_payload(operation.payload):
                return {"status": "authorization_unconfirmed"}
            if operation.payload.get("status") != "queued":
                return {"status": "already_authorized_or_settled"}
            try:
                if _close_unserviceable_design(session, design):
                    return {"status": "blocked"}
                _authority(design, operation, scope, session=session, artifact_directory=artifact_directory)
                if not _capacity(session, design, operation):
                    operation.payload = {**operation.payload, "dispatch_status": "pending", "published_at": None, "due_at": (utcnow() + timedelta(seconds=2)).isoformat()}
                    return {"status": "capacity_wait"}
                # A failed capacity/price/storage check must not commit a partial
                # reservation or consume counters without dispatch authority.
                with session.begin_nested():
                    state = VisualState(**design.state)
                    if not state.paid_admitted:
                        expiry = operation.payload.get("initial_admission_expires_at") or (design.preflight or {}).get("initial_admission_expires_at")
                        if not expiry or aware(datetime.fromisoformat(expiry)) <= utcnow():
                            raise VisualLimit("Initial visual admission authorization expired")
                        state.admitted_at, state.paid_admitted = utcnow().timestamp(), True
                        design.state = asdict(state)
                    # Validate existing deployment exposure before even
                    # provider preflight; unreadable liabilities are not zero.
                    _policy_checks(session, _proxy(design), Decimal("0"))
                    provider = provider_registry[operation.provider] if provider_registry is not None else configured_image_provider(operation.provider, design.configuration)
                    if operation.payload["kind"] in {"vision", "edit"}:
                        version_id = operation.payload.get("target_version_id") or operation.payload.get("parent_version_id")
                        version = session.get(DesignVersion, version_id) if version_id else None
                        if version is not None:
                            from round_robin.visual.service import version_feedback_findings
                            version_feedback_findings(session, design, version)
                    packet = _packet(session, design, operation, provider, artifact_directory)
                    if hasattr(provider, "preflight"):
                        bound = design.configuration["vision_prices"][operation.provider] if operation.payload["kind"] == "vision" else design.configuration["generation_price_usd" if operation.payload["kind"] == "generation" else "edit_price_usd"]
                        provider.preflight(packet, ImageAuthorization(operation.id, design.owner_id, design.id, bound, design.execution_fence, design.deployment_epoch, operation.payload["step_id"]))
                    packet["timeout_seconds"] = _request_allowance(session, design, operation)
                    authorization = _reserve_operation(session, design, operation, state, request_seconds=packet["timeout_seconds"])
                    if operation.payload["kind"] in {"generation", "edit"}:
                        storage_id = f"imagestorage_{operation.id}"
                        storage = reserve_asset_storage(session, design, storage_id)
                        storage.payload = {**storage.payload, "external_operation_id": operation.id}
                    else:
                        storage_id = None
                    design.state = asdict(state)
                    authorized_request_hash, authorized_provider = request_hash(packet), operation.provider
                    operation.payload = {**operation.payload, "status": "authorized", "dispatch_status": "done", "request_hash": authorized_request_hash, "lease_owner": token, "lease_expires_at": (utcnow() + timedelta(seconds=IMAGE_LEASE_SECONDS)).isoformat(), "receipt": packet.get("receipt"), "storage_reservation_id": storage_id, "authorized_at": utcnow().isoformat()}
            except (VisualLimit, ImageFundingLocked, BudgetDenied, ValueError, KeyError) as error:
                operation.payload = {**operation.payload, "status": "blocked", "dispatch_status": "done", "error_code": type(error).__name__}
                return {"status": "blocked"}
    except SQLAlchemyError:
        return {"status": "authorization_unconfirmed"}
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(operation_id, token, session_factory, stop), daemon=True)
    result = failure = None
    heartbeat_started = False
    try:
        try:
            heartbeat.start()
            heartbeat_started = True
        except RuntimeError:
            raise ProviderFailure("image_heartbeat_unavailable", may_have_sent=False)
        result = provider.review(packet, authorization) if packet["kind"] == "vision" else provider.submit(packet, authorization, idempotency_key=None)
        if not isinstance(result, dict):
            raise ProviderFailure("invalid_image_result")
        if result.get("status") != "completed":
            raise ProviderFailure("refusal" if result.get("status") == "refused" else "invalid_image_result",
                                  cost_usd=result.get("actual_usd"), provider_request_id=result.get("provider_request_id"))
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        failure = error if isinstance(error, ProviderFailure) else ProviderFailure("image_operation_unknown")
        result = None
    finally:
        stop.set()
        if heartbeat_started:
            heartbeat.join(timeout=1)
    for attempt in range(3):
        try:
            with session_factory() as session, session.begin():
                design, operation, scope = _locked_design(session, operation_id)
                if not design:
                    return {"status": "persistence_unconfirmed"}
                if not operation or not _readable_operation_payload(operation.payload):
                    # The request may already have been sent. Preserve its
                    # reservation and do not infer a result from damaged state.
                    return {"status": "persistence_unconfirmed"}
                cost = result.get("actual_usd") if result else failure.cost_usd
                if cost is None and failure and not failure.may_have_sent:
                    cost = "0"
                try:
                    if cost is not None:
                        cost = str(amount(cost))
                except BudgetDenied:
                    # Invalid usage cannot turn an already sent request into a
                    # free attempt or prevent the terminal receipt from saving.
                    cost = None
                ledger = session.scalar(select(BudgetOperation).where(BudgetOperation.id == operation_id).with_for_update().execution_options(populate_existing=True))
                payload = operation.payload
                if (ledger is None or ledger.run_id != authorization.session_id or ledger.owner_id != authorization.owner_id
                        or ledger.kind != f"visual_{packet['kind']}" or design.id != authorization.session_id
                        or design.owner_id != authorization.owner_id or operation.provider != authorized_provider
                        or payload.get("session_id") != authorization.session_id or payload.get("owner_id") != authorization.owner_id
                        or payload.get("step_id") != authorization.step_id or payload.get("fence") != authorization.fence
                        or payload.get("epoch") != authorization.epoch or payload.get("request_hash") != authorized_request_hash
                        or ledger.reserved_usd != authorization.bound_usd):
                    return {"status": "persistence_unconfirmed"}
                if not isinstance(ledger.metadata_json, dict):
                    # A provider response belongs to this immutable authorization,
                    # but its ledger metadata is damaged. Retain it byte-for-byte,
                    # record proven usage, and fence publication and new dispatch.
                    state = VisualState(**design.state)
                    state_operation = state.operations.get(operation_id)
                    if (not isinstance(state_operation, dict) or state_operation.get("kind") != packet["kind"]
                            or state_operation.get("step_id") != authorization.step_id
                            or state_operation.get("bound") != authorization.bound_usd):
                        return {"status": "persistence_unconfirmed"}
                    state.settle(operation_id, str(cost) if cost is not None else None)
                    reconcile_operation(session, operation_id, cost, "reported" if cost is not None else "unknown")
                    if design.status == "active":
                        state.status, state.fence = "quarantined", state.fence + 1
                        design.status, design.execution_fence = "quarantined", state.fence
                    design.state = asdict(state)
                    request_id = result.get("provider_request_id") if result else failure.provider_request_id
                    operation.payload = {**payload, "status": "fenced", "error_code": "ledger_integrity_failed", "provider_request_id": request_id,
                                         "usage_status": "reported" if cost is not None else "unknown", "lease_owner": None, "lease_expires_at": None}
                    return {"status": "ledger_integrity_unavailable"}
                if operation.payload.get("status") != "authorized":
                    if ledger and ledger.status in {"reserved", "unknown"} and cost is not None:
                        reconcile_operation(session, operation_id, cost, "reported")
                        state = VisualState(**design.state)
                        if operation_id in state.operations:
                            state.settle(operation_id, str(cost))
                            design.state = asdict(state)
                        _release_unused_pool(session, design)
                        return {"status": "late_usage_reconciled"}
                    return {"status": "late_or_already_settled"}
                ledger = reconcile_operation(session, operation_id, cost, "reported" if cost is not None else "unknown")
                request_id = result.get("provider_request_id") if result else failure.provider_request_id
                ledger.metadata_json = {**ledger.metadata_json, "provider_request_id": request_id}
                state = VisualState(**design.state)
                state.settle(operation_id, str(cost) if cost is not None else None)
                eligible = operation.payload.get("lease_owner") == token
                try:
                    _authority(design, operation, scope, session=session, artifact_directory=artifact_directory)
                    eligible = eligible and utcnow().timestamp() - state.admitted_at < 1800 and aware(datetime.fromisoformat(operation.payload["lease_expires_at"])) > utcnow()
                except (VisualLimit, ImageFundingLocked):
                    eligible = False
                design.state = asdict(state)
                if eligible and result:
                    try:
                        # A malformed artifact/critique must roll back only
                        # publication, preserving the usage just reconciled.
                        with session.begin_nested():
                            if packet["kind"] == "vision":
                                version = session.get(DesignVersion, packet["receipt"]["version_id"])
                                receipt = PixelReceipt(**{**packet["receipt"], "views": tuple(packet["receipt"]["views"])})
                                commit_review(session, design, version, Critique.model_validate(result["critique"]), receipt, expected_fence=authorization.fence)
                            else:
                                raster = validate_canonical_raster(result["image_bytes"])
                                version = commit_version(session, design, raster, artifact_directory / "visual", expected_fence=authorization.fence, parent_version_id=operation.payload.get("parent_version_id") if packet["kind"] == "edit" else None, generator_provider=operation.provider, source_kind="edit" if packet["kind"] == "edit" else "generated", operation_id=operation_id, storage_reservation_id=operation.payload["storage_reservation_id"])
                                version.provenance = {**version.provenance, "review_pool_tag": operation.payload.get("review_pool_tag") or operation.payload["step_id"], "provider_request_id": request_id, "edit_intent": operation.payload.get("edit_intent", []), "finding_dispositions": operation.payload.get("finding_dispositions", {}), **({"edit_bundle": packet["edit_bundle"], "edit_bundle_hash": packet["edit_bundle_hash"]} if packet.get("edit_bundle") else {})}
                                queue_version_reviews(session, design, version)
                        operation.payload = {**operation.payload, "status": "completed", "provider_request_id": request_id, "usage_status": "reported" if cost is not None else "unknown", "lease_owner": None, "lease_expires_at": None}
                    except (VisualLimit, ValueError, KeyError, TypeError):
                        remove_unreferenced_assets(session, design, artifact_directory)
                        failure = ProviderFailure("invalid_image_result")
                if not eligible or failure:
                    operation.payload = {**operation.payload, "status": "failed" if eligible and failure else "fenced", "error_code": failure.code if failure else "late_image_result_fenced", "provider_request_id": request_id, "lease_owner": None, "lease_expires_at": None}
                    _release_linked_storage(session, design, operation)
                _release_unused_pool(session, design)
            return {"status": "persisted"}
        except (SQLAlchemyError, OSError) as error:
            if isinstance(error, OSError):
                from round_robin.operations.durability import mark_failure
                mark_failure(session_factory, "artifact_storage_failed", artifact_directory=artifact_directory, control_directory=settings.durability_control_directory)
            if attempt == 2:
                return {"status": "persistence_unconfirmed"}
            time.sleep(.25 * (attempt + 1))
        except (VisualLimit, ValueError):
            return {"status": "invalid_result_not_published"}
    return {"status": "persistence_unconfirmed"}


def dispatch_pending_image_operations(*, session_factory=SessionLocal, sender=None):
    sender = sender or (lambda identity: image_app.send_task("round_robin.visual.execute", args=[identity], queue=os.environ.get("ROUND_ROBIN_IMAGE_TASK_QUEUE", "round_robin_images"), retry=False))
    published = 0
    with session_factory() as session:
        ids = [operation.id for operation in session.scalars(select(DesignOperation)) if _readable_operation_payload(operation.payload) and operation.payload.get("status") == "queued" and operation.payload.get("dispatch_status", "pending") in {"pending", "published"}]
    for identity in ids:
        try:
            with session_factory() as session, session.begin():
                design, operation, scope = _locked_design(session, identity)
                if not design or not _readable_operation_payload(operation.payload) or operation.payload.get("status") != "queued":
                    continue
                if _close_unserviceable_design(session, design):
                    continue
                _authority(design, operation, scope, session=session)
                due = operation.payload.get("due_at")
                if due and aware(datetime.fromisoformat(due)) > utcnow():
                    continue
                last = operation.payload.get("published_at")
                if last and aware(datetime.fromisoformat(last)) + timedelta(seconds=15) > utcnow():
                    continue
                sender(identity)
                operation.payload = {**operation.payload, "dispatch_status": "published", "published_at": utcnow().isoformat()}
                published += 1
        except (SQLAlchemyError, VisualLimit, ImageFundingLocked, Exception):
            # Original durable intent remains. No paid authorization occurred.
            continue
    return published


def _release_linked_storage(session, design, operation):
    storage_id = operation.payload.get("storage_reservation_id")
    if storage_id is None:
        return
    if not isinstance(storage_id, str) or not storage_id:
        operation.payload = {**operation.payload, "storage_release_unconfirmed": True}
        return
    storage = session.scalar(select(DesignOperation).where(DesignOperation.id == storage_id).with_for_update().execution_options(populate_existing=True))
    if (storage and storage.session_id == design.id and isinstance(storage.payload, dict)
            and storage.payload.get("kind") == "asset_reservation"
            and storage.payload.get("external_operation_id") == operation.id
            and storage.payload.get("status") == "reserved"):
        storage.payload = {**storage.payload, "status": "released"}
    else:
        operation.payload = {**operation.payload, "storage_release_unconfirmed": True}


def reconcile_image_operations(*, session_factory=SessionLocal):
    changed = 0
    with session_factory() as session:
        ids = [operation.id for operation in session.scalars(select(DesignOperation)) if _readable_operation_payload(operation.payload) and operation.payload.get("status") == "authorized"]
    for identity in ids:
        try:
            with session_factory() as session, session.begin():
                design, operation, scope = _locked_design(session, identity)
                if not design or not _readable_operation_payload(operation.payload) or operation.payload.get("status") != "authorized":
                    continue
                expiry = operation.payload.get("lease_expires_at")
                if expiry and aware(datetime.fromisoformat(expiry)) > utcnow():
                    continue
                reconcile_operation(session, identity, None, "unknown")
                state = VisualState(**design.state)
                state.settle(identity, None)
                design.state = asdict(state)
                operation.payload = {**operation.payload, "status": "unknown", "dispatch_status": "done", "lease_owner": None, "lease_expires_at": None, "error_code": "image_worker_lost_after_authorization"}
                _release_linked_storage(session, design, operation)
            changed += 1
        except (SQLAlchemyError, BudgetDenied, VisualLimit, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            # Roll back the entire ambiguous operation; never invent a settlement.
            # Reconciliation of unrelated sessions can proceed independently.
            continue
    with session_factory() as session:
        scopes = [(design.id, design.owner_id, design.meeting_id) for design in session.scalars(select(DesignSession))]
    for design_id, owner_id, meeting_id in scopes:
        try:
            with session_factory() as session, session.begin():
                lock_policies(session, owner_id)
                session.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
                design = session.scalar(select(DesignSession).where(DesignSession.id == design_id).with_for_update().execution_options(populate_existing=True))
                if not design:
                    continue
                closed = _close_unserviceable_design(session, design)
            changed += int(closed)
        except (SQLAlchemyError, BudgetDenied, VisualLimit, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            # Leave damaged state and reservations unchanged on transaction failure.
            continue
    return changed


@image_app.task(name="round_robin.visual.execute", bind=True)
def execute_image_task(self, operation_id: str):
    return execute_image_operation(operation_id)
