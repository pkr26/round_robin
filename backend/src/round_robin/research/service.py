"""Durable public-search authorization and fenced evidence commits.

The task can be redelivered, but an ambiguous search authorization is never sent
again. Public retrieval failure leaves a visible gap and permits conditional
text review. No private brief text or location becomes a search fallback.
"""

from datetime import datetime, timedelta
import hashlib
import json
import time
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from round_robin.database.models import BudgetOperation, EvidenceRecord, Outbox, Run, utcnow
from round_robin.database.services import append_event, enqueue
from round_robin.database.session import SessionLocal
from round_robin.orchestration.admission import TERMINAL, lock_work_run
from round_robin.orchestration.budget import BudgetDenied, amount, aware, authorize_operation, reconcile_operation, remaining_active
from round_robin.schemas.views import StoredEvidence
from .fetcher import FetchAllowance, FetchRejected, FetchUnavailable, collect_source, exact_passages, public_addresses, validate_target
from .queries import PublicScope, QueryRejected, render_query
from .search import BraveSearch


def proposal_packet_bytes(session, run, passages):
    """Exact initial serialization keeps downstream discussion space reserved."""
    from round_robin.orchestration.reporting import comparison_as_of, comparison_data
    from round_robin.providers.base import ProviderRequest
    from round_robin.schemas.briefs import DraftBrief
    from .discussion import current_assessments
    from .reuse import claim_packet
    brief = run.configuration.get("resolved_brief", run.input_snapshot)
    as_of = comparison_as_of(run)
    _, calculations = comparison_data(DraftBrief.model_validate(brief), as_of=as_of)
    claims = claim_packet(session, run, imported_only=True)
    references = {item["id"] for item in [*passages, *calculations, *claims]}
    packet = ProviderRequest(run.id, "step_research_fit", "proposal", "anthropic", brief, passages, [], frozenset(references), 3072, 90,
        calculations=tuple(calculations), claims=tuple(claims), cost_as_of=as_of.isoformat() if as_of else None,
        assessments=tuple(current_assessments(run, {item["id"]: item for item in passages}))).packet()
    return len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())


def scope_for_run(run) -> PublicScope:
    brief = run.input_snapshot
    terms = tuple((item["id"], item["text"]) for item in brief.get("public_research_terms", []))
    # Private `location` is deliberately never copied into this separate scope.
    return PublicScope(terms=terms, region=run.configuration.get("research", {}).get("approved_public_region", ""))


def query_plan(scope: PublicScope) -> list[dict]:
    groups, current = [], []
    templates = scope.template_ids
    if not templates:
        raise QueryRejected("No approved public query templates are available")
    for identity, _ in scope.terms:
        proposed = [*current, identity]
        rendered = [render_query(scope, term_ids=proposed, template_id=template) for template in templates]
        if current and any(len(query) > 600 or len(query.split()) > 75 for query in rendered):
            groups.append(current)
            current = [identity]
        else:
            current = proposed
        if any(len(query) > 600 or len(query.split()) > 75 for query in (render_query(scope, term_ids=current, template_id=template) for template in templates)):
            raise QueryRejected("An approved public term and region cannot fit the bounded search contract")
    if current:
        groups.append(current)
    if len(groups) > 6:
        raise QueryRejected("Approved public terms exceed the six-query allowance")
    # Rotate templates across groups so truncation never drops every
    # counterevidence query or silently omits the final approved term group.
    return [{"template_id": template, "term_ids": group, "query": render_query(scope, term_ids=group, template_id=template)} for turn in range(len(templates)) for index, group in enumerate(groups) for template in [templates[(index + turn) % len(templates)]]][:6]


def schedule_research(session, run) -> bool:
    """Called before proposal dispatch; True means the durable task is pending."""
    state = run.configuration.get("research_state")
    if state == "done":
        return False
    if state in {"pending", "running"}:
        return True
    scope = scope_for_run(run)
    policy = run.configuration.get("research", {})
    if not scope.terms:
        run.configuration = {**run.configuration, "research_state": "done", "research_summary": {"status": "not_requested", "queries": 0, "pages": 0}}
        return False
    if run.configuration.get("mode", "simulated") != "live" or not policy.get("enabled") or not policy.get("pricing_version") or policy.get("per_request_usd") is None:
        run.configuration = {**run.configuration, "research_state": "done", "research_summary": {"status": "unavailable", "reason": "simulation_or_unconfigured_research", "queries": 0, "pages": 0}}
        append_event(session, run, "research.unavailable", {"reason": "simulation_or_unconfigured_research", "verified": False})
        return False
    amount(policy["per_request_usd"])
    try:
        plan = query_plan(scope)
    except QueryRejected:
        run.configuration = {**run.configuration, "research_state": "done", "research_summary": {"status": "unavailable", "reason": "approved_query_exceeds_bounds", "queries": 0, "pages": 0}}
        append_event(session, run, "research.unavailable", {"reason": "approved_query_exceeds_bounds", "verified": False})
        return False
    run.configuration = {**run.configuration, "research_state": "pending", "public_scope_hash": scope.fingerprint, "research_plan": plan, "research_summary": {"status": "pending", "queries": 0, "pages": 0}}
    enqueue(session, run, "research")
    return True


def reconcile_public_research(session, run) -> bool:
    if run.configuration.get("research_state") == "pending":
        if research_seconds_available(run) > 0:
            return False
        finish_unstarted_research(session, run)
        return True
    if run.configuration.get("research_state") != "running":
        return False
    lease = run.configuration.get("research_lease_until")
    try:
        if lease and aware(datetime.fromisoformat(lease)) > utcnow():
            return False
    except (ValueError, TypeError):
        pass  # an invalid lease cannot retain permission to dispatch
    for operation_id in run.configuration.get("research_operation_ids", []):
        operation = session.get(BudgetOperation, operation_id)
        if operation and operation.status == "reserved":
            reconcile_operation(session, operation_id, None, "unknown")
    run.configuration = {**run.configuration, "research_state": "done", "research_lease_id": None, "research_summary": {**run.configuration.get("research_summary", {}), "status": "limited", "reason": "research_worker_lost_unknown_outcome"}}
    append_event(session, run, "research.unavailable", {"reason": "research_worker_lost_unknown_outcome", "verified": False})
    enqueue(session, run)
    return True


def research_seconds_available(run) -> float:
    reserve = run.configuration.get("finish_policy", {}).get("reserved_active_seconds", 190)
    if run.configuration.get("discussion_policy"):
        reserve = max(190, reserve)
    return max(0, min(70, remaining_active(run) - reserve - 25))


def finish_unstarted_research(session, run):
    """Settle known-unsent research without consuming protected finishing time."""
    run.configuration = {**run.configuration, "research_state": "done", "research_lease_id": None,
        "research_summary": {"status": "unavailable", "reason": "finish_time_reserve", "queries": 0, "pages": 0}}
    append_event(session, run, "research.unavailable", {"reason": "finish_time_reserve", "verified": False})
    enqueue(session, run)


def execute_research(run_id: str, *, session_factory=SessionLocal, search=None, transport=None, resolver=public_addresses) -> bool:
    with session_factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        if not run or run.status != "running" or run.configuration.get("research_state") != "pending":
            return False
        from round_robin.orchestration.configuration import verify_frozen_briefs, verify_saved_discussion
        try:
            verify_frozen_briefs(run)
            verify_saved_discussion(session, run)
        except BudgetDenied as failure:
            from round_robin.orchestration.controller import finalize_run
            finalize_run(session, run, reason=str(failure))
            return False
        scope = scope_for_run(run)
        if scope.fingerprint != run.configuration.get("public_scope_hash"):
            raise BudgetDenied("public_scope_changed")
        plan = run.configuration["research_plan"]
        policy = run.configuration["research"]
        initial_fence, initial_epoch = run.execution_fence, run.deployment_epoch
        available = research_seconds_available(run)
        existing_count = len(list(session.scalars(select(EvidenceRecord.id).where(EvidenceRecord.run_id == run.id))))
        packet_capacity = max(0, 24 - existing_count)
        if not packet_capacity:
            run.configuration = {**run.configuration, "research_state": "done", "research_lease_id": None,
                "research_summary": {"status": "limited", "reason": "passage_packet_limit", "queries": 0, "pages": 0, "limitations": ["passage_packet_limit"]}}
            append_event(session, run, "research.unavailable", {"reason": "passage_packet_limit", "verified": False})
            enqueue(session, run)
            return True
        from round_robin.providers.base import MAX_INITIAL_PACKET_BYTES, ProviderFailure
        from round_robin.reports.read_adapters import read_stored_evidence
        initial_passages = [read_stored_evidence(row, run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id)
            for row in session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id))]
        try:
            has_context_room = proposal_packet_bytes(session, run, initial_passages) + 512 < MAX_INITIAL_PACKET_BYTES
        except ProviderFailure:
            has_context_room = False
        if not has_context_room:
            run.configuration = {**run.configuration, "research_state": "done", "research_lease_id": None,
                "research_summary": {"status": "limited", "reason": "downstream_context_reserve", "queries": 0, "pages": 0, "limitations": ["downstream_context_reserve"]}}
            append_event(session, run, "research.unavailable", {"reason": "downstream_context_reserve", "verified": False})
            enqueue(session, run)
            return True
        if available <= 0:
            finish_unstarted_research(session, run)
            return False
        deadline = time.monotonic() + available
        lease_id = uuid4().hex
        run.configuration = {**run.configuration, "research_state": "running", "research_lease_id": lease_id, "research_lease_until": (utcnow() + timedelta(seconds=110)).isoformat(), "research_operation_ids": []}
    def eligible(session, current, *, unsent_operation_id=None):
        def settle_unsent():
            if current is None or unsent_operation_id is None:
                return
            operation = session.get(BudgetOperation, unsent_operation_id)
            if operation is not None and operation.run_id == current.id and operation.kind == "public_search" and operation.status == "reserved":
                reconcile_operation(session, unsent_operation_id, None, "proven_not_sent")

        try:
            active = current is not None and current.status == 'running' and current.execution_fence == initial_fence and current.deployment_epoch == initial_epoch and current.configuration.get('research_state') == 'running' and current.configuration.get('research_lease_id') == lease_id and aware(datetime.fromisoformat(current.configuration['research_lease_until'])) > utcnow()
        except (KeyError, ValueError, TypeError):
            active = False
        if not active:
            settle_unsent()
            if current is not None and current.status == "cancel_requested" and unsent_operation_id is not None:
                from round_robin.orchestration.controller import finalize_cancelled_safely
                finalize_cancelled_safely(session, current)
            return False
        try:
            verify_frozen_briefs(current)
            verify_saved_discussion(session, current)
        except BudgetDenied as failure:
            settle_unsent()
            from round_robin.orchestration.controller import finalize_run
            finalize_run(session, current, reason=str(failure))
            return False
        return True
    adapter = search or BraveSearch()
    allowance = FetchAllowance(max_pages=min(8, packet_capacity))
    sources, failures, queries, objectives = [], [], 0, []
    for index, query in enumerate(plan[:6]):
        objective = {"template_id": query["template_id"], "term_ids": query["term_ids"], "queried": False, "pages_attempted": 0, "sources_retrieved": 0}
        objectives.append(objective)
        if allowance.pages >= allowance.max_pages:
            failures.append("unfulfilled_research_objective")
            continue
        if time.monotonic() >= deadline:
            failures.append("research_deadline")
            continue
        # Allocate before consuming hits. Remaining objectives retain at least
        # their fair share, even when the first query returns eight candidates.
        pages_before = allowance.pages
        pages_for_query = max(1, (allowance.max_pages - allowance.pages) // max(1, min(6, len(plan)) - index))
        operation_id = "search_" + hashlib.sha256(f"{run_id}:{index}:{scope.fingerprint}".encode()).hexdigest()[:48]
        try:
            with session_factory() as session, session.begin():
                run = lock_work_run(session, run_id)
                if not eligible(session, run):
                    break
                if session.get(BudgetOperation, operation_id):
                    failures.append("search_outcome_unknown_not_repeated")
                    continue
                if render_query(scope, term_ids=query["term_ids"], template_id=query["template_id"]) != query["query"]:
                    raise QueryRejected("Frozen query identity mismatch")
                authorize_operation(session, run, operation_id, "public_search", policy["per_request_usd"], model_call=False)
                run.configuration = {**run.configuration, "research_operation_ids": [*run.configuration.get("research_operation_ids", []), operation_id]}
                append_event(session, run, "research.query_authorized", {"operation_id": operation_id, "template_id": query["template_id"], "term_ids": query["term_ids"]})
            with session_factory() as session, session.begin():
                run = lock_work_run(session, run_id)
                if not eligible(session, run, unsent_operation_id=operation_id):
                    return False
            queries += 1
            objective["queried"] = True
            hits = adapter.search(query["query"], timeout=min(5, max(.05, deadline - time.monotonic())))
            # Search does not return a final provider billing receipt. Retain the
            # conservative authorization until genuine settlement is available.
            with session_factory() as session, session.begin():
                run = lock_work_run(session, run_id)
                reconcile_operation(session, operation_id, None, "unknown")
                if not eligible(session, run):
                    return False
            for hit in hits:
                if allowance.pages >= allowance.max_pages or allowance.pages - pages_before >= pages_for_query or time.monotonic() >= deadline:
                    break
                with session_factory() as session, session.begin():
                    run = lock_work_run(session, run_id)
                    if not eligible(session, run):
                        return False
                try:
                    public_url = validate_target(hit.url, resolver, resolution_timeout=min(5, max(.05, deadline - time.monotonic()))).url
                except (FetchRejected, FetchUnavailable):
                    failures.append("discovery_url_rejected")
                    continue
                candidate_id = "candidate_" + hashlib.sha256(public_url.encode()).hexdigest()[:40]
                if candidate_id in allowance.registered_urls:
                    continue
                allowance.registered_urls[candidate_id] = public_url
                with session_factory() as session, session.begin():
                    run = lock_work_run(session, run_id)
                    if not eligible(session, run):
                        return False
                    run.configuration = {**run.configuration, "research_candidates": {**run.configuration.get("research_candidates", {}), candidate_id: {"url": public_url, "origin": "server_recorded_search_result"}}, "research_summary": {"status": "running", "queries": queries, "pages": allowance.pages + 1}}
                with session_factory() as session, session.begin():
                    run = lock_work_run(session, run_id)
                    if not eligible(session, run):
                        return False
                try:
                    source = collect_source(candidate_id, allowance, transport=transport, resolver=resolver, timeout=min(5, max(.05, deadline - time.monotonic())))
                    sources.append((source, hit.title))
                    objective["sources_retrieved"] += 1
                except (FetchRejected, FetchUnavailable):
                    failures.append("source_unavailable_or_rejected")
        except (BudgetDenied, FetchRejected, FetchUnavailable, QueryRejected):
            failures.append("search_unavailable_or_ineligible")
            # A reserved operation remains uncertain after any ambiguous crash.
        objective["pages_attempted"] = allowance.pages - pages_before
        if not objective["sources_retrieved"]:
            failures.append("research_objective_without_new_source")
    with session_factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        if not eligible(session, run):
            return False
        if scope_for_run(run).fingerprint != scope.fingerprint:
            raise BudgetDenied("public_scope_changed")
        existing = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id)))
        from round_robin.reports.read_adapters import read_stored_evidence
        from round_robin.providers.base import MAX_INITIAL_PACKET_BYTES, ProviderFailure
        packet_passages = [read_stored_evidence(row, run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id) for row in existing]
        remaining = max(0, 24 - len(existing))
        content_groups = {record.sha256: record.content.get("origin_group", record.id) for record in existing}
        brief = run.configuration.get("resolved_brief", run.input_snapshot)
        relevance_terms = [item.get("description", "") for item in brief.get("hard_requirements", [])] + [item["text"] for item in brief.get("public_research_terms", [])]
        for source_index, (source, title) in enumerate(sources):
            if remaining == 0:
                failures.append("passage_packet_limit")
                break
            # Two passages per source keep counterevidence room; an excerpt is
            # explicitly not a promise to have read or verified the full page.
            source_slots = max(1, min(2, remaining // max(1, len(sources) - source_index)))
            for passage in exact_passages(source, maximum=source_slots, terms=relevance_terms):
                identity = "evidence_" + hashlib.sha256(f"{run.id}:{source.url}:{passage['locator']}".encode()).hexdigest()[:40]
                if session.get(EvidenceRecord, identity):
                    continue
                group = content_groups.setdefault(passage["sha256"], source.origin_group)
                content = {"id": identity, "run_id": run.id, "owner_id": run.owner_id, "source_title": title or source.url, "url": source.url, "origin": "retrieved", "verified_at": source.fetched_at, "origin_group": group, "time_sensitive": True, **passage}
                try:
                    content = StoredEvidence.model_validate(content).model_dump(mode="json", exclude_unset=True)
                except ValidationError:
                    failures.append("source_metadata_or_passage_rejected")
                    continue
                try:
                    fits = proposal_packet_bytes(session, run, [*packet_passages, content]) <= MAX_INITIAL_PACKET_BYTES
                except ProviderFailure:
                    fits = False
                if not fits:
                    failures.append("downstream_context_reserve")
                    continue
                session.add(EvidenceRecord(id=identity, run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id, sha256=passage["sha256"], content=content))
                packet_passages.append(content)
                remaining -= 1
        run.configuration = {**run.configuration, "research_state": "done", "research_lease_id": None, "research_summary": {"status": "limited" if failures else "retrieved_unverified", "queries": queries, "pages": allowance.pages, "source_count": len(sources), "limitations": sorted(set(failures)), "objectives": objectives}}
        append_event(session, run, "research.settled", {"queries": queries, "pages": allowance.pages, "source_count": len(sources), "semantic_verification": False, "limitations": sorted(set(failures))})
        enqueue(session, run)
    return True
