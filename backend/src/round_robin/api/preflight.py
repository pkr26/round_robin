from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from round_robin.database.models import BriefRevision, Draft, PreflightRecord, Run, Upload, utcnow
from round_robin.database.services import append_event, canonical_hash, enqueue, lock_policies, new_id
from round_robin.schemas.application import PreflightRequest, StartRun
from round_robin.schemas.provider_configuration import ProviderConfiguration
from round_robin.schemas.views import PreflightView, RunView
from round_robin.research.reuse import ClaimRejected, import_parent_items, selected_parent_manifest
from .auth import get_db, require_owner
from .commands import aware, command_begin, command_commit, fail, owned_meeting
from .lineage import capture_parent_replacement, replace_active_parent

router = APIRouter()
PROVIDERS = ["openai", "anthropic", "google"]


def run_configuration(settings, mode: str) -> dict:
    # Live is fail closed: a key alone never constitutes pricing or funding approval.
    configuration = {"mode": mode, "funded": False, "deployment_epoch": settings.deployment_epoch, "protocol": "independent_review_provisional", "models": {provider: f"simulated-{provider}-v1" for provider in PROVIDERS}, "pricing": {}, "capabilities": {provider: {"structured_text": True} for provider in PROVIDERS}, "limits": {"max_requests": 24, "max_calls_per_step": 2, "active_seconds": 600, "per_run_usd": "2", "owner_daily_usd": "10", "owner_monthly_usd": "50"}, "finish_policy": {"reserved_usd": "0.40", "reserved_requests": 2, "reserved_active_seconds": 190, "version": "runtime-finish-v1"}, "fallbacks": {stage: list(PROVIDERS) for stage in ("intake", "synthesis", "audit")}}
    if mode == "live" and settings.provider_config_file is not None:
        try:
            from round_robin.operations.config_files import read_operator_configuration
            frozen = ProviderConfiguration.model_validate_json(read_operator_configuration(settings.provider_config_file))
            if frozen.expires_at <= utcnow():
                raise ValueError("configuration_expired")
        except Exception:
            fail("provider_configuration_invalid", "Operator provider configuration is missing, expired or invalid", 422)
        configuration = {**frozen.model_dump(mode="json"), "operator_configuration_hash": canonical_hash(frozen.model_dump(mode="json")), "mode": "live", "funded": bool(settings.paid_access_enabled), "deployment_epoch": settings.deployment_epoch}
        # Freeze the lower accepted owner ceilings; current policy can lower them again.
        from decimal import Decimal
        configuration["limits"]["owner_daily_usd"] = str(min(Decimal(configuration["limits"]["owner_daily_usd"]), Decimal(configuration["aggregate_funding"]["owner_daily_usd"])))
        configuration["limits"]["owner_monthly_usd"] = str(min(Decimal(configuration["limits"]["owner_monthly_usd"]), Decimal(configuration["aggregate_funding"]["owner_monthly_usd"])))
    from round_robin.providers.http import request_contract_hash
    configuration["request_contract_hash"] = request_contract_hash()
    from round_robin.orchestration.protocol import policy_descriptor
    configuration["discussion_policy"] = policy_descriptor()
    return configuration


def selected_manifest(session: Session, meeting_id: str, owner: str, brief: dict) -> list[dict]:
    manifest = []
    for input_id in brief.get("selected_attachment_ids", []):
        item = session.get(Upload, input_id)
        if not item or item.meeting_id != meeting_id or item.owner_id != owner or item.status != "ready" or not item.sha256 or set(item.recipients) != set(PROVIDERS):
            fail("input_scope_invalid", "Only ready, immutable same-meeting inputs shared with all three providers may be selected")
        manifest.append({"id": item.id, "sha256": item.sha256, "recipients": PROVIDERS, "byte_size": item.byte_size})
    return manifest


def input_packet_error(session, meeting_id, owner, brief, manifest, reuse):
    from round_robin.providers.base import ProviderFailure
    from round_robin.orchestration.budget import BudgetDenied
    from round_robin.orchestration.controller import plan_input_evidence
    from round_robin.research.currencies import minor_units
    try:
        for currency in {brief["currency"], *(component["currency"] for option in brief["options"] for component in option["cost_components"])}:
            minor_units(currency)
        # Imported parent passages occupy the same 24-record packet. Their
        # final child IDs are allocated at Start, but their count is frozen now.
        future_run_id = "run_" + "0" * 32
        resolved, _, records, aliases = plan_input_evidence(session, brief, run_id=future_run_id, meeting_id=meeting_id,
            owner_id=owner, manifest=manifest,
            existing_ids={f"parent_passage_{index}" for index, item in enumerate(reuse) if item["kind"] == "evidence"})
        # Admit encoded provider input, not character counts. Keep mandatory
        # downstream discussion space separate from the frozen initial input.
        from copy import deepcopy
        from round_robin.providers.base import MAX_INITIAL_PACKET_BYTES, ProviderFailure, ProviderRequest
        from round_robin.orchestration.reporting import comparison_data
        from round_robin.schemas.briefs import DraftBrief
        resolved = deepcopy(resolved)
        for option in resolved["options"]:
            for component in option["cost_components"]:
                component["source_ref"] = aliases[component["source_ref"]]
        import hashlib
        passages = [{"id": identity, "run_id": future_run_id, "owner_id": owner,
            "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(), "origin": "user_supplied",
            "verification_status": "user_supplied", "verified_at": utcnow().isoformat(),
            "time_sensitive": True, "origin_group": identity, **metadata} for identity, text, metadata in records]
        imported_claims = []
        from round_robin.database.models import EvidenceRecord
        from round_robin.database.claim_models import ClaimVersion
        for item in reuse:
            model = EvidenceRecord if item["kind"] == "evidence" else ClaimVersion
            row = session.get(model, item["id"])
            if row is not None:
                (passages if item["kind"] == "evidence" else imported_claims).append(row.content)
        from zoneinfo import ZoneInfo
        _, calculations = comparison_data(DraftBrief.model_validate(resolved), as_of=utcnow().astimezone(ZoneInfo(resolved.get("timezone", "America/Phoenix"))).date())
        refs = {item["id"] for item in passages + imported_claims + calculations}
        packet = ProviderRequest(future_run_id, "step_preflight", "proposal", "openai", resolved,
            passages, [], frozenset(refs), 3072, 90, calculations=tuple(calculations), claims=tuple(imported_claims)).packet()
        import json
        encoded = len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
        # Imported items gain child IDs/provenance/hash metadata at Start.
        if encoded + 500 * len(reuse) > MAX_INITIAL_PACKET_BYTES:
            raise BudgetDenied("context_limit_downstream_reserve")
    except ProviderFailure:
        return "context_limit_packet_bytes", "The complete UTF-8 evidence packet exceeds the provider context limit. Select shorter excerpts before starting."
    except BudgetDenied as error:
        messages = {
            "context_limit_selected_passages": "Selected attachments, structured quotes and reused passages exceed the 24-passage evidence limit. Select shorter excerpts or fewer sources.",
            "context_limit_structured_quote": "A structured quote exceeds 2,000 characters. Split its cost components across distinct source references or shorten their text.",
            "hard_requirement_limit_preserve_explicit_budget_deadline": "Leave room within 20 hard requirements for the explicit budget and deadline.",
            "context_limit_downstream_reserve": "Selected input leaves insufficient room for mandatory proposal, objection and audit context. Select shorter excerpts before starting.",
        }
        return str(error), messages.get(str(error), "Selected input content no longer matches its immutable scope; review the attachments.")
    except ValueError:
        return "unsupported_currency", "Use a currency with a supported ISO 4217 minor unit; unsupported currency codes cannot be priced."
    return None


@router.post("/api/meetings/{meeting_id}/preflight", response_model=PreflightView)
def preflight(meeting_id: str, body: PreflightRequest, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    with session.begin():
        kind = "preflight"
        command_body = body.model_dump(mode="json", exclude={"selected_parent_items"} if not body.selected_parent_items else set())
        prior = command_begin(session, request, owner, body.command_id, kind, {"meeting_id": meeting_id, **command_body})
        if prior:
            return prior.result
        policies = lock_policies(session, owner) if body.mode == "live" else None
        owned_meeting(session, meeting_id, owner, lock=True)
        draft = session.get(Draft, meeting_id)
        if draft.revision != body.expected_draft_revision:
            fail("draft_conflict", "Review the latest draft revision before preflight", retry_scope="read")
        manifest = selected_manifest(session, meeting_id, owner, draft.payload)
        try:
            reuse = selected_parent_manifest(session, draft, owner, [item.model_dump(mode="json") for item in body.selected_parent_items])
        except ValueError:
            fail("reuse_scope_invalid", "Select exact immutable items from this draft's parent in the same meeting")
        parent_replacement = capture_parent_replacement(session, draft, owner)
        configuration = run_configuration(request.app.state.settings, body.mode)
        blockers, warnings = [], []
        packet_error = input_packet_error(session, meeting_id, owner, draft.payload, manifest, reuse)
        if packet_error:
            blockers.append(packet_error[1])
        if body.mode == "live" and not configuration.get("funded"):
            blockers.append("Live provider access requires frozen validated pricing, capabilities and an explicitly funded operator policy; this configuration is not funded.")
        if policies and any(policy.spending_locked for policy in policies):
            blockers.append("Operator or owner spending policy is locked; configure funding and explicitly unlock before Start.")
        candidates = [option for option in draft.payload["options"] if option["role"] == "candidate"]
        if len(candidates) < 2 or draft.payload.get("horizon_months") is None:
            warnings.append("An intake confirmation will be required before proposals; missing facts remain unknown.")
        if not any(option["role"] == "status_quo" for option in draft.payload["options"]):
            warnings.append("No status-quo option was supplied; intake must consider keeping or deferring the current approach.")
        now = utcnow()
        payload = {"id": new_id("preflight"), "meeting_id": meeting_id, "draft_revision": draft.revision, "draft_hash": canonical_hash(draft.payload), "configuration_hash": canonical_hash(configuration), "fingerprint": canonical_hash({"brief": draft.payload, "inputs": manifest, "configuration": configuration}), "input_manifest": manifest, "recipients": PROVIDERS, "approved_public_terms": draft.payload["public_research_terms"], "public_research": {"recipient": "Brave Search" if configuration.get("research", {}).get("enabled") else None, "approved_public_region": configuration.get("research", {}).get("approved_public_region", ""), "enabled": bool(configuration.get("research", {}).get("enabled")), "query_templates": ["comparison_v1", "costs_v1", "counterevidence_v1"]}, "protocol": configuration["protocol"], "mode": body.mode, "limits": configuration["limits"], "finish_policy": configuration["finish_policy"], "configuration": configuration, "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat(), "initial_admission_window_minutes": 30, "consumed_by_run_id": None, "blockers": blockers, "warnings": warnings, "paid_access_enabled": configuration.get("funded", False)}
        payload.update(reuse_manifest=reuse, parent_replacement=parent_replacement, parent_run_id=draft.parent_run_id)
        payload["fingerprint"] = canonical_hash({"brief": draft.payload, "inputs": manifest, "configuration": configuration,
            "reuse_manifest": reuse, "parent_run_id": draft.parent_run_id, "parent_replacement": parent_replacement})
        row = PreflightRecord(id=payload["id"], meeting_id=meeting_id, owner_id=owner, draft_revision=draft.revision, draft_hash=payload["draft_hash"], configuration_hash=payload["configuration_hash"], payload=payload, created_at=now, expires_at=now + timedelta(minutes=10))
        session.add(row)
        command_commit(session, request, owner, body.command_id, kind, payload, resource_id=row.id, meeting_id=meeting_id)
        return payload


@router.post("/api/meetings/{meeting_id}/runs", status_code=202, response_model=RunView)
def start_run(meeting_id: str, body: StartRun, request: Request, owner=Depends(require_owner), session: Session = Depends(get_db)):
    from .events import run_view
    with session.begin():
        command_body = body.model_dump(mode="json", exclude={"replace_parent_run_version"} if body.replace_parent_run_version is None else set())
        prior = command_begin(session, request, owner, body.command_id, "start", {"meeting_id": meeting_id, **command_body})
        if prior:
            return prior.result
        policies = lock_policies(session, owner)
        meeting = owned_meeting(session, meeting_id, owner, lock=True)
        draft = session.get(Draft, meeting_id)
        row = session.scalar(select(PreflightRecord).where(PreflightRecord.id == body.preflight_id, PreflightRecord.meeting_id == meeting_id, PreflightRecord.owner_id == owner).with_for_update())
        if row is None:
            fail("preflight_stale", "Preflight not found for this meeting")
        if row.consumed_by_run_id:
            fail("preflight_consumed", "A preflight authorizes only one run; obtain a new preflight")
        if aware(row.expires_at) <= utcnow():
            fail("preflight_expired", "The ten-minute preflight expired; review a fresh preflight")
        if draft.revision != body.expected_draft_revision or row.draft_revision != draft.revision or row.draft_hash != canonical_hash(draft.payload):
            fail("preflight_stale", "Draft changed after preflight; review it again")
        if row.configuration_hash != canonical_hash(run_configuration(request.app.state.settings, row.payload["mode"])):
            fail("preflight_stale", "Configuration changed after preflight")
        if row.payload["input_manifest"] != selected_manifest(session, meeting_id, owner, draft.payload):
            fail("input_scope_invalid", "Selected inputs changed after preflight")
        try:
            accepted_reuse = row.payload.get("reuse_manifest", [])
            selections = [{key: item[key] for key in ("id", "kind", "version", "sha256")} for item in accepted_reuse]
            if row.payload.get("parent_run_id") != draft.parent_run_id or accepted_reuse != selected_parent_manifest(session, draft, owner, selections):
                raise ClaimRejected("Parent selection changed after preflight")
        except ValueError:
            fail("reuse_scope_invalid", "Selected parent content changed after preflight; review it again")
        packet_error = input_packet_error(session, meeting_id, owner, draft.payload, row.payload["input_manifest"], accepted_reuse)
        if packet_error:
            fail(*packet_error)
        if row.payload["blockers"]:
            fail("spending_locked", row.payload["blockers"][0])
        if row.payload["mode"] == "live" and any(policy.spending_locked for policy in policies):
            fail("spending_locked", "Operator or owner spending policy was locked after preflight")
        replace_active_parent(session, draft, owner, row.payload.get("parent_replacement"), body.replace_parent_run_version)
        from round_robin.orchestration.admission import ensure_queue_capacity
        from round_robin.orchestration.budget import BudgetDenied
        try:
            ensure_queue_capacity(session, owner, owner_limit=request.app.state.settings.max_queued_per_owner)
        except BudgetDenied:
            fail("quota_exceeded", "Owner or deployment queue limit reached", 429)
        now = utcnow()
        configuration = dict(row.payload["configuration"])
        configuration["input_manifest"] = row.payload["input_manifest"]
        run = Run(id=new_id("run"), meeting_id=meeting_id, owner_id=owner, parent_run_id=draft.parent_run_id, preflight_id=row.id, input_snapshot=draft.payload, configuration=configuration, initial_admission_expires_at=now + timedelta(minutes=30), deployment_epoch=request.app.state.settings.deployment_epoch, execution_fence=meeting.execution_fence)
        session.add(run)
        session.flush()
        try:
            imported_reuse = import_parent_items(session, run, accepted_reuse)
        except ClaimRejected:
            fail("reuse_scope_invalid", "Selected parent content changed after preflight; review it again")
        run.configuration = {**configuration, "reuse_manifest": imported_reuse}
        session.add(BriefRevision(id=new_id("brief"), run_id=run.id, version=1, kind="submitted", payload=draft.payload, command_id=str(body.command_id)))
        row.consumed_by_run_id = run.id
        row.payload = {**row.payload, "consumed_by_run_id": run.id}
        append_event(session, run, "run.queued", {"stage": "intake", "mode": configuration["mode"]})
        enqueue(session, run)
        session.flush()
        result = run_view(session, run)
        command_commit(session, request, owner, body.command_id, "start", result, resource_id=run.id, meeting_id=meeting_id, status_code=202)
        return result
