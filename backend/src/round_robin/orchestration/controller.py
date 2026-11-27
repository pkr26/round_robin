"""Transactional barriers and deterministic finalization from committed work."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json

from sqlalchemy import select

from round_robin.database.models import Attempt, BudgetOperation, EvidenceRecord, Meeting, ReportVersion, Run, Step, Upload, utcnow
from round_robin.database.services import append_event, enqueue, new_id
from round_robin.providers.base import PROVIDERS
from round_robin.reports.read_adapters import read_stored_evidence
from round_robin.schemas.briefs import DraftBrief, deadline_requirement_description
from .admission import TERMINAL, admit_run, capacity_delay_seconds, check_eligible
from .budget import BudgetDenied, _exact_money, _policy_checks, amount, aware, checked_finish_hold, freeze_active, validate_run_ledger, operations_exposure, release_unused_finish, remaining_active
from .reporting import build_invalid_brief_report, build_report
from .protocol import correction_required, next_stage, optional_cutoff_reached


def _evidence_id(run_id, alias):
    return "evidence_" + hashlib.sha256(f"{run_id}:{alias}".encode()).hexdigest()[:40]


def validated_evidence(session, run):
    try:
        records = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        return [read_stored_evidence(record, run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id) for record in records]
    except (ValueError, RecursionError):
        raise BudgetDenied("evidence_integrity_failed") from None


def _parent_context(session, run):
    """Read only scoped prior context, retaining its authoritative identity."""
    if not run.parent_run_id:
        return None, None, None, False, False
    if run.parent_run_id == run.id:
        return None, None, None, True, True
    parent = session.execute(select(Run.id, Run.owner_id, Run.meeting_id).where(Run.id == run.parent_run_id)).first()
    if not parent or parent.owner_id != run.owner_id or parent.meeting_id != run.meeting_id:
        return None, None, None, True, True

    parent_brief, brief_unavailable = None, False
    try:
        snapshot = session.scalar(select(Run.input_snapshot).where(
            Run.id == parent.id, Run.owner_id == run.owner_id, Run.meeting_id == run.meeting_id))
        DraftBrief.model_validate(snapshot)
        parent_brief = snapshot
    except (ValueError, TypeError, RecursionError):
        brief_unavailable = True

    parent_report_id, parent_report, report_unavailable = None, None, False
    identity = session.execute(select(ReportVersion.id, ReportVersion.version).join(
        Run, Run.id == ReportVersion.run_id).where(Run.id == parent.id,
        Run.owner_id == run.owner_id, Run.meeting_id == run.meeting_id
    ).order_by(ReportVersion.version.desc()).limit(1)).first()
    if identity is None:
        report_unavailable = True
    else:
        try:
            from pydantic import TypeAdapter
            from round_robin.schemas.common import Id
            from round_robin.reports.read_adapters import read_runtime_report
            if not isinstance(identity.id, str):
                raise ValueError("Prior report identity is invalid")
            parent_report_id = TypeAdapter(Id).validate_python(identity.id)
            scoped_parent = session.scalar(select(Run).where(
                Run.id == parent.id, Run.owner_id == run.owner_id, Run.meeting_id == run.meeting_id
            ).execution_options(populate_existing=True))
            if scoped_parent is None:
                raise ValueError("Prior run no longer exists in scope")
            row = session.scalar(select(ReportVersion).where(
                ReportVersion.id == parent_report_id, ReportVersion.run_id == parent.id,
                ReportVersion.version == identity.version).execution_options(populate_existing=True))
            if row is None:
                raise ValueError("Prior report no longer exists")
            parent_report = read_runtime_report(session, row)
        except (ValueError, TypeError, RecursionError):
            report_unavailable = True
    if parent_report_id is not None:
        confirmed = session.scalar(select(ReportVersion.id).join(
            Run, Run.id == ReportVersion.run_id).where(
            ReportVersion.id == parent_report_id, ReportVersion.version == identity.version,
            Run.id == parent.id, Run.owner_id == run.owner_id, Run.meeting_id == run.meeting_id))
        if confirmed is None:
            parent_report_id, parent_report, report_unavailable = None, None, True
    if session.scalar(select(Run.id).where(Run.id == parent.id, Run.owner_id == run.owner_id,
                                           Run.meeting_id == run.meeting_id)) is None:
        return None, None, None, True, True
    return parent_report_id, parent_report, parent_brief, report_unavailable, brief_unavailable


def resolved_brief(snapshot):
    """Expose explicit budget/date fields as assessed hard requirements."""
    brief = deepcopy(snapshot)
    requirements = brief.setdefault("hard_requirements", [])
    identifiers = {item["id"] for item in requirements}
    added = []
    def append_requirement(base_id, description, metric, operator, threshold=None):
        if len(requirements) >= 20:
            raise BudgetDenied("hard_requirement_limit_preserve_explicit_budget_deadline")
        identity = base_id
        counter = 1
        while identity in identifiers:
            identity = f"{base_id}_{counter}"
            counter += 1
        requirements.append({"id": identity, "description": description, "origin": "user_supplied", "metric": metric, "operator": operator, "threshold": threshold})
        identifiers.add(identity)
        added.append(identity)
    budget = brief.get("budget")
    if budget is not None and not any(item["metric"] == "total_cost" and item["operator"] == "lte" and item.get("threshold") is not None and Decimal(item["threshold"]) <= Decimal(budget) for item in requirements):
        append_requirement("requirement_server_budget", f"Total cost must not exceed the explicitly supplied budget of {budget} {brief['currency']} over the confirmed horizon.", "total_cost", "lte", budget)
    deadline = brief.get("deadline")
    if deadline:
        description = deadline_requirement_description(deadline)
        if not any(item["metric"] == "manual" and item["operator"] == "manual" and item["description"] == description for item in requirements):
            append_requirement("requirement_server_deadline", description, "manual", "manual")
    return DraftBrief.model_validate(brief).model_dump(mode="json"), added


def plan_input_evidence(session, snapshot, *, run_id, meeting_id, owner_id, manifest, existing_ids=()):
    """Validate the exact packet before admission and again before persistence."""
    brief, added_requirements = resolved_brief(snapshot)
    groups = {}
    for option in brief.get("options", []):
        for component in option.get("cost_components", []):
            groups.setdefault(component["source_ref"], []).append({"option": option["label"], "component": component})
    records = []
    aliases = {}
    for alias, facts in groups.items():
        evidence_id = _evidence_id(run_id, f"structured:{alias}")
        text = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        if len(text) > 2000:
            raise BudgetDenied("context_limit_structured_quote")
        aliases[alias] = evidence_id
        records.append((evidence_id, text, {"original_alias": alias, "source_title": "User-entered structured quote facts", "locator": "Frozen structured cost fields", "scope": "Exact user-entered values; no independent supplier verification.", "origin_group": _evidence_id(run_id, "common_user_entered_structured_quote_origin")}))
    if isinstance(manifest, dict):
        manifest = manifest.get("items", [])
    if {item["id"] for item in manifest} != set(brief.get("selected_attachment_ids", [])):
        raise BudgetDenied("input_manifest_changed")
    for item in manifest:
        upload = session.get(Upload, item["id"])
        if not upload or upload.status != "ready" or upload.meeting_id != meeting_id or upload.owner_id != owner_id or set(upload.recipients) != set(PROVIDERS) or upload.sha256 != item.get("sha256", item.get("content_hash")):
            raise BudgetDenied("input_scope_invalid")
        text = upload.canonical_text or ""
        if hashlib.sha256(text.encode()).hexdigest() != upload.sha256:
            raise BudgetDenied("input_hash_invalid")
        for offset in range(0, len(text), 2000):
            records.append((_evidence_id(run_id, f"upload:{upload.id}:{offset}"), text[offset:offset + 2000], {"source_title": upload.filename, "locator": f"UTF-8 text characters {offset + 1}–{min(offset + 2000, len(text))}", "scope": "Exact selected uploaded text; instructions remain untrusted data.", "input_id": upload.id, "origin_group": "origin_" + upload.sha256}))
    if len(set(existing_ids) | {record[0] for record in records}) > 24:
        raise BudgetDenied("context_limit_selected_passages")
    return brief, added_requirements, records, aliases


def prepare_evidence(session, run):
    if run.configuration.get("evidence_prepared"):
        return
    existing_ids = set(session.scalars(select(EvidenceRecord.id).where(EvidenceRecord.run_id == run.id)))
    brief, added_requirements, records, aliases = plan_input_evidence(session, run.input_snapshot,
        run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id,
        manifest=run.configuration.get("input_manifest", []), existing_ids=existing_ids)
    for option in brief.get("options", []):
        for component in option.get("cost_components", []):
            component["source_ref"] = aliases[component["source_ref"]]
    for evidence_id, text, metadata in records:
        digest = hashlib.sha256(text.encode()).hexdigest()
        if session.get(EvidenceRecord, evidence_id) is None:
            content = {"id": evidence_id, "run_id": run.id, "owner_id": run.owner_id, "text": text, "sha256": digest, "origin": "user_supplied", "verification_status": "user_supplied", "verified_at": utcnow().isoformat(), "time_sensitive": True, "origin_group": evidence_id, **metadata}
            session.add(EvidenceRecord(id=evidence_id, run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id, content=content, sha256=digest))
    run.configuration = {**run.configuration, "resolved_brief": brief, "source_aliases": aliases, "evidence_prepared": True, "resolved_requirement_additions": added_requirements}
    append_event(session, run, "evidence.committed", {"passage_count": len(records), "independently_verified": False})


def intake_evidence_view(session, run):
    """Validate selected text for intake without freezing an unconfirmed brief."""
    existing = validated_evidence(session, run)
    brief, _, records, aliases = plan_input_evidence(session, run.input_snapshot,
        run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id,
        manifest=run.configuration.get("input_manifest", []), existing_ids={item["id"] for item in existing})
    for option in brief.get("options", []):
        for component in option.get("cost_components", []):
            component["source_ref"] = aliases[component["source_ref"]]
    packets = {item["id"]: item for item in existing}
    for identity, text, metadata in records:
        packets[identity] = {"id": identity, "run_id": run.id, "owner_id": run.owner_id,
            "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "origin": "user_supplied", "verification_status": "user_supplied",
            "verified_at": utcnow().isoformat(), "time_sensitive": True,
            "origin_group": identity, **metadata}
    return brief, list(packets.values())


def _stage_steps(session, run):
    stage = "proposal" if run.stage == "proposals" else run.stage
    iteration = int(run.configuration.get("stage_iteration", 0))
    from .saved_state import read_saved_steps
    return read_saved_steps(session, select(Step).where(Step.run_id == run.id, Step.stage == stage, Step.stage_iteration == iteration).order_by(Step.created_at, Step.id))


def _ensure_steps(session, run):
    if run.stage == "proposals":
        brief = DraftBrief.model_validate(run.input_snapshot)
        if not 2 <= sum(option.role == "candidate" for option in brief.options) <= 4 or not brief.horizon_months:
            raise BudgetDenied("confirmed_brief_incomplete")
        prepare_evidence(session, run)
        from round_robin.research.service import schedule_research
        if schedule_research(session, run):
            return []
    steps = _stage_steps(session, run)
    if steps:
        for step in steps:
            if step.status == "pending":
                # Recovery after a committed step intent was lost from Redis.
                from round_robin.database.models import Outbox
                controls = run.configuration.get("step_dispatch", {})
                if not isinstance(controls, dict):
                    continue
                control = controls.get(step.id, {})
                if not isinstance(control, dict):
                    continue
                generation = control.get("generation", 0)
                if type(generation) is not int or generation < 0:
                    continue
                # Unknown or excessive saved work remains a conservative fence.
                outstanding = False
                payloads = None
                try:
                    payloads = session.scalars(select(Outbox.payload).where(
                        Outbox.run_id == run.id, Outbox.step_id == step.id,
                        Outbox.kind == "execute", Outbox.status.in_(["pending", "published"]),
                    ).limit(257).execution_options(yield_per=1))
                    for index, payload in enumerate(payloads):
                        if index == 256 or not isinstance(payload, dict):
                            outstanding = True
                            break
                        old = payload.get("dispatch_generation", 0)
                        if type(old) is not int or old < 0 or old >= generation:
                            outstanding = True
                            break
                except (ValueError, RecursionError):
                    outstanding = True
                finally:
                    if payloads is not None:
                        payloads.close()
                if not outstanding:
                    not_before = control.get("not_before")
                    if not_before is not None and not isinstance(not_before, str):
                        continue
                    if not_before:
                        try:
                            parsed = datetime.fromisoformat(not_before)
                            if parsed.utcoffset() is None:
                                continue
                        except (ValueError, OverflowError):
                            continue
                    enqueue(session, run, "execute", step_id=step.id)
        return steps
    if run.stage == "proposals":
        slots = [(provider, provider) for provider in PROVIDERS]
        stage = "proposal"
    else:
        stage = run.stage
        provider = run.configuration.get("moderators", {}).get(stage, "openai")
        if stage == "audit":
            from .saved_state import read_saved_step
            synth = read_saved_step(session, select(Step).where(Step.run_id == run.id, Step.stage == "synthesis", Step.status == "succeeded").order_by(Step.stage_iteration.desc()).limit(1))
            if synth is None or synth.stage_iteration != int(run.configuration.get("stage_iteration", 0)):
                raise BudgetDenied("synthesis_unavailable")
            provider = next((candidate for candidate in run.configuration.get("fallbacks", {}).get("audit", ["google", "anthropic", "openai"]) if candidate != synth.provider), None)
        if provider not in PROVIDERS:
            raise BudgetDenied("independent_auditor_unavailable")
        slots = [(stage, provider)]
    for slot, provider in slots:
        step = Step(id=new_id("step"), run_id=run.id, stage=stage, role_slot_id=slot, provider=provider, status="pending", calls=0, infrastructure_claims=0, generation=run.recovery_generation, stage_iteration=int(run.configuration.get("stage_iteration", 0)))
        session.add(step)
        session.flush()
        enqueue(session, run, "execute", step_id=step.id)
        steps.append(step)
    append_event(session, run, "stage.started", {"stage": run.stage, "roles": [step.role_slot_id for step in steps]})
    return steps


@_exact_money
def _reserve_correction(session, run):
    """Fund one additional synthesis/audit pair without renewing any limit.

    The declared finish envelope is an upper bound, not a promise to spend it:
    each executor must still prove its actual request fits its allocated half.
    A second pair counts against the original run, owner and deployment budgets,
    original deadline, and the same logical-role call/claim ceilings.
    """
    if run.configuration.get("stage_iteration", 0) != 0:
        return False
    limits = run.configuration.get("limits", {})
    from .saved_state import read_saved_steps
    steps = read_saved_steps(session, select(Step).where(Step.run_id == run.id))
    for stage in ("synthesis", "audit"):
        prior = [step for step in steps if step.stage == stage]
        if (sum(step.calls for step in prior) >= min(2, limits.get("max_calls_per_step", 2))
                or sum(step.infrastructure_claims for step in prior) >= 3):
            return False
    operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id)))
    if sum(operation.metadata_json.get("model_call") is True for operation in operations) + 2 > min(24, limits.get("max_requests", 24)):
        return False
    finish = run.configuration.get("finish_policy", {})
    if remaining_active(run) <= max(190, int(finish.get("reserved_active_seconds", 190))) + capacity_delay_seconds(session) + 25:
        return False
    try:
        hold = checked_finish_hold(session, run, lock=True)
    except BudgetDenied:
        return False
    if hold is None or hold.status != "held" or hold.metadata_json.get("roles_remaining"):
        return False
    reserved = amount("0" if run.configuration.get("mode", "simulated") == "simulated" else finish.get("reserved_usd", "0.40"))
    try:
        _policy_checks(session, run, reserved)
    except BudgetDenied:
        return False
    hold.reserved_usd = str(amount(hold.reserved_usd) + reserved)
    hold.metadata_json = {**hold.metadata_json, "roles_remaining": ["synthesis", "audit"], "initial_reserved_usd": str(reserved), "correction_reserved_usd": str(reserved)}
    run.configuration = {**run.configuration, "stage_iteration": 1}
    run.stage = next_stage("audit", correction_admitted=True)
    append_event(session, run, "audit.correction_scheduled", {"stage_iteration": 1, "reserved_usd": str(reserved)})
    return True


@_exact_money
def finalize_run(session, run, *, reason=None, status=None):
    meeting = session.get(Meeting, run.meeting_id)
    if not meeting or meeting.deleted_at:
        # A deletion reconciler owns purging. No late report materialization.
        return
    if run.status in TERMINAL and (run.report_version or run.configuration.get("ledger_integrity_failed") or run.configuration.get("saved_state_unavailable")) and status is None:
        return
    operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id)))
    try:
        checked_finish_hold(session, run, lock=True)
        validate_run_ledger(operations, simulated=run.configuration.get("mode", "simulated") == "simulated")
    except BudgetDenied:
        # The amount and even the number of calls cannot be certified. Fence
        # work while retaining every hold and any previously saved reports.
        cancelled = run.status in {"cancel_requested", "cancelled"}
        try:
            freeze_active(run)
        except BudgetDenied:
            run.active_started_at = None
            run.configuration = {**run.configuration, "active_clock_invalid": True}
        run.configuration = {**run.configuration, "ledger_integrity_failed": True}
        run.status = "cancelled" if cancelled else "failed"
        run.stop_reason = (run.stop_reason or "user_cancellation") if cancelled else "ledger_integrity_failed"
        run.stage = "finished"
        run.version += 1
        run.execution_fence += 1
        from .saved_state import read_saved_steps
        for step in read_saved_steps(session, select(Step).where(Step.run_id == run.id)):
            if step.status in {"pending", "claimed", "authorized"}:
                step.status, step.error_code = "skipped", "ledger_integrity_failed"
                step.lease_owner, step.lease_expires_at = None, None
        append_event(session, run, "run.accounting_unavailable", {"message": "The current report and usage are unavailable because saved accounting records could not be verified.", "status": run.status})
        return
    try:
        from .configuration import verify_frozen_briefs
        verify_frozen_briefs(run)
        invalid_brief = False
    except BudgetDenied:
        invalid_brief = True
        run.configuration = {**run.configuration, "invalid_frozen_inputs": True}
    try:
        from .configuration import verify_saved_discussion
        verify_saved_discussion(session, run)
        invalid_discussion = False
    except BudgetDenied:
        invalid_discussion = True
        run.configuration = {**run.configuration, "discussion_integrity_failed": True}
    invalid_saved = invalid_brief or invalid_discussion
    if not invalid_saved and not run.configuration.get("evidence_prepared"):
        try:
            prepare_evidence(session, run)
        except (BudgetDenied, ValueError):
            # Failed intake/context validation still gets an honest status
            # report. Never create references to unstored hypothetical evidence.
            pass
    from .saved_state import read_saved_steps
    steps = read_saved_steps(session, select(Step).where(Step.run_id == run.id).order_by(Step.created_at, Step.id))
    substantive = any(step.status == "succeeded" and step.stage != "intake" for step in steps)
    if run.status in {"cancel_requested", "cancelled"}:
        status, reason = "cancelled", reason or run.stop_reason or "user_cancellation"
    elif invalid_brief:
        status, reason = "failed", "invalid_frozen_inputs"
    elif invalid_discussion:
        status, reason = "failed", "discussion_integrity_failed"
    elif status is None:
        status = "completed_with_limitations" if substantive else "failed"
    try:
        freeze_active(run)
    except BudgetDenied as failure:
        if str(failure) != "clock_regression":
            raise
        # An invalid interval cannot be measured honestly. Preserve the already
        # counted intervals and permanently fence execution without preventing
        # cancellation or a deterministic saved partial report.
        run.active_started_at = None
        run.configuration = {**run.configuration, "active_clock_invalid": True}
        if status != "cancelled":
            status = "completed_with_limitations" if substantive and not invalid_saved else "failed"
            reason = "clock_regression"
    run.status, run.stop_reason = status, reason or "protocol_complete"
    run.stage = "finished"
    run.version += 1
    for step in steps:
        if step.status in {"pending", "claimed", "authorized"}:
            step.status = "skipped"
            step.error_code = run.stop_reason
            step.lease_owner, step.lease_expires_at = None, None
    operations = list(session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id)))
    unknown = [operation.id for operation in operations if operation.kind != "finish_hold" and operation.status in {"reserved", "unknown"}]
    from .policies import retryable_steps
    keep_finish = bool(retryable_steps(session, run, steps=steps))
    if not keep_finish:
        release_unused_finish(session, run)
    report_id = new_id("report")
    version = run.report_version + 1
    usage = {"simulated": run.configuration.get("mode", "simulated") == "simulated", "status": "unknown" if unknown else "reported", "actual_cost": None if unknown else format(sum((Decimal(operation.settled_usd or "0") for operation in operations if operation.kind != "finish_hold"), Decimal("0")), "f"), "conservative_exposure_usd": format(operations_exposure(operations), "f"), "unknown_operation_ids": unknown, "request_count": sum(operation.metadata_json.get("model_call") is True for operation in operations), "currency": "USD"}
    parent_report_id, parent_report, parent_brief, parent_report_unavailable, parent_brief_unavailable = _parent_context(session, run)
    try:
        evidence = validated_evidence(session, run)
        if invalid_saved:
            claims = []
        else:
            from round_robin.research.reuse import claim_packet
            claims = claim_packet(session, run)
    except (BudgetDenied, ValueError):
        evidence = []
        claims = []
        run.configuration = {**run.configuration, "comparison_unavailable_reason": "evidence_integrity_failed"}
        if status != "cancelled":
            run.stop_reason = "evidence_integrity_failed"
            status = "completed_with_limitations" if substantive and not invalid_saved else "failed"
            run.status = status
    published_at = utcnow()
    if invalid_saved:
        report = build_invalid_brief_report(run, steps, status=status, stop_reason=run.stop_reason, report_id=report_id, version=version, usage=usage, evidence=evidence, parent_report=parent_report, parent_report_id=parent_report_id, parent_report_unavailable=parent_report_unavailable, parent_brief_unavailable=parent_brief_unavailable, invalid_brief=invalid_brief, invalid_discussion=invalid_discussion, published_at=published_at)
    else:
        report = build_report(run, steps, status=status, stop_reason=run.stop_reason, report_id=report_id, version=version, usage=usage, evidence=evidence, claims=claims, parent_report=parent_report, parent_report_id=parent_report_id, parent_brief=parent_brief, parent_report_unavailable=parent_report_unavailable, parent_brief_unavailable=parent_brief_unavailable, published_at=published_at)
    session.add(ReportVersion(id=report_id, run_id=run.id, version=version, event_cutoff=run.event_sequence + 1, content=report, created_at=published_at))
    run.report_version = version
    synthesis = next((step for step in reversed(steps) if step.stage == "synthesis" and step.status == "succeeded"), None)
    audit = next((step for step in reversed(steps) if step.stage == "audit" and step.status == "succeeded"), None)
    mandatory_complete = bool(status == "completed" and synthesis and audit and synthesis.provider != audit.provider and audit.result_json["output"].get("audit_passed") is True and not audit.result_json["output"].get("critical_defects") and len({step.provider for step in steps if step.stage == "proposal" and step.status == "succeeded"}) == 3)
    qualifying = bool(report["comparison"] and (any(calculation["output"] is not None for calculation in report["calculations"]) or report["next_evidence"]))
    append_event(session, run, "report.published", {"report_id": report_id, "version": version, "execution_status": status, "readiness": report["readiness"], "active_seconds": run.active_seconds, "qualifying_content": qualifying, "mandatory_work_complete": mandatory_complete, "simulated": run.configuration.get("mode", "simulated") == "simulated"})


def fence_unreadable_saved_state(session, run):
    """Stop without creating a report or changing unknown/held operations."""
    cancelled = run.status in {"cancel_requested", "cancelled"}
    try:
        freeze_active(run)
    except BudgetDenied:
        run.active_started_at = None
        run.configuration = {**run.configuration, "active_clock_invalid": True}
    run.status = "cancelled" if cancelled else "failed"
    run.stop_reason = (run.stop_reason or "user_cancellation") if cancelled else "saved_state_integrity_failed"
    run.stage = "finished"
    run.version += 1
    run.execution_fence += 1
    run.configuration = {**run.configuration, "saved_state_unavailable": True}
    append_event(session, run, "run.saved_state_unavailable", {"message": "The current report is unavailable because saved state could not be verified."})


def finalize_cancelled_safely(session, run):
    """Finish an already committed cancellation without rewriting corrupt steps."""
    from .saved_state import SavedStateUnreadable
    try:
        with session.begin_nested():
            finalize_run(session, run)
    except SavedStateUnreadable:
        fence_unreadable_saved_state(session, run)


def advance_run(session, run):
    if run.status == "cancel_requested":
        finalize_cancelled_safely(session, run)
        return
    if run.status in TERMINAL or run.status == "awaiting_input":
        return
    try:
        if not admit_run(session, run):
            enqueue(session, run, due_at=utcnow() + timedelta(seconds=5))
            return
        check_eligible(session, run)
        if run.stage == "proposals":
            brief = DraftBrief.model_validate(run.input_snapshot)
            if not 2 <= sum(option.role == "candidate" for option in brief.options) <= 4 or not brief.horizon_months:
                raise BudgetDenied("confirmed_brief_incomplete")
            prepare_evidence(session, run)
        steps = _ensure_steps(session, run)
        if run.stage == "proposals" and optional_cutoff_reached(remaining_seconds=remaining_active(run),
                reserved_finish_seconds=run.configuration.get("finish_policy", {}).get("reserved_active_seconds", 190),
                capacity_delay_seconds=capacity_delay_seconds(session)):
            for step in steps:
                if step.status in {"pending", "claimed", "authorized"}:
                    step.status, step.error_code = "skipped", "mandatory_finish_cutoff"
                    step.lease_owner, step.lease_expires_at = None, None
                    append_event(session, run, "step.skipped", {"step_id": step.id, "stage": step.stage,
                        "provider": step.provider, "error_code": "mandatory_finish_cutoff"})
        if not steps or any(step.status not in {"succeeded", "failed", "skipped"} for step in steps):
            return
        key = f"{run.stage}:{run.recovery_generation}:{run.configuration.get('stage_iteration', 0)}"
        closed = run.configuration.get("closed_barriers", [])
        if key in closed:
            return
        run.configuration = {**run.configuration, "closed_barriers": [*closed, key]}
        append_event(session, run, "stage.settled", {"stage": run.stage, "succeeded": sum(step.status == "succeeded" for step in steps), "scheduled": len(steps)})
        if run.stage == "intake":
            successful = next((step for step in steps if step.status == "succeeded"), None)
            if not successful:
                finalize_run(session, run, reason="intake_unavailable")
                return
            questions = successful.result_json["output"].get("questions", [])[:3]
            changes = successful.result_json["output"].get("proposed_changes", [])
            if changes and not questions:
                questions = ["Review the proposed brief changes and explicitly confirm the final brief before comparison."]
            run.configuration = {**run.configuration, "proposed_brief": run.input_snapshot, "proposed_changes": changes, "questions": questions}
            transition = next_stage("intake", [successful.provider], confirmation_required=bool(questions))
            if transition == "awaiting_input":
                waiting = list(session.scalars(select(Run.id).where(Run.owner_id == run.owner_id, Run.status == "awaiting_input", Run.id != run.id)))
                if len(waiting) >= 3:
                    raise BudgetDenied("awaiting_input_quota_exceeded")
                freeze_active(run)
                run.status = "awaiting_input"
                run.input_expires_at = utcnow() + timedelta(hours=24)
                run.version += 1
                append_event(session, run, "brief.confirmation_required", {"questions": questions, "expires_at": run.input_expires_at.isoformat(), "active_seconds": run.active_seconds})
                return
            run.stage = transition
        elif run.stage == "proposals":
            transition = next_stage("proposals", [step.provider for step in steps if step.status == "succeeded"])
            if transition == "publication":
                finalize_run(session, run, reason="insufficient_independent_proposals")
                return
            run.stage = transition
        elif run.stage == "synthesis":
            transition = next_stage("synthesis", [step.provider for step in steps if step.status == "succeeded"])
            if transition == "publication":
                finalize_run(session, run, reason="synthesis_unavailable")
                return
            run.stage = transition
        elif run.stage == "audit":
            audit = next((step for step in steps if step.status == "succeeded"), None)
            good = audit and audit.result_json["output"].get("audit_passed") is True and not audit.result_json["output"].get("critical_defects")
            from round_robin.research.discussion import discussion_issues
            from .saved_state import read_saved_steps
            issues = discussion_issues(run, read_saved_steps(session, select(Step).where(Step.run_id == run.id, Step.status == "succeeded")))
            good = good and not any(item.get("severity") in {"material", "critical"} and item.get("status") in {"open", "answered_pending_review"} for item in issues)
            needs_correction = audit and correction_required(audit_passed=audit.result_json["output"].get("audit_passed"),
                critical_defects=audit.result_json["output"].get("critical_defects", []),
                iteration=run.configuration.get("stage_iteration", 0), open_objections=issues)
            if needs_correction and _reserve_correction(session, run):
                run.version += 1
                _ensure_steps(session, run)
                return
            incomplete = any(step.status != "succeeded" for step in read_saved_steps(session, select(Step).where(Step.run_id == run.id)))
            finalize_run(session, run, reason="protocol_complete" if good else "independent_audit_incomplete_or_negative", status="completed" if good and not incomplete else "completed_with_limitations")
            return
        else:
            raise BudgetDenied("unsupported_stage")
        run.version += 1
        _ensure_steps(session, run)
    except (BudgetDenied, ValueError) as exc:
        code = str(exc) if isinstance(exc, BudgetDenied) else "invalid_frozen_inputs"
        if code == "clock_regression":
            run.active_started_at = None
            run.configuration = {**run.configuration, "active_clock_invalid": True}
        if code == "dispatch_paused":
            return
        finalize_run(session, run, reason=code)
