"""Deterministic read adaptation; never rewrites a saved report's original bytes."""
import math
from collections import Counter, defaultdict
from copy import deepcopy

from round_robin.database.models import Attempt, Event, EvidenceRecord, Run, Step
from round_robin.schemas.runtime import RuntimeDecisionReport
from round_robin.schemas.views import StoredEvidence
from sqlalchemy import select


class UnreadableReport(ValueError):
    """A saved report failed safe adaptation; private validation inputs stay local."""


def _validate_finite_numbers(value):
    """Reject non-JSON numbers before JSON-mode serialization can erase them."""
    pending = [value]
    seen = set()
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise UnreadableReport("Saved report contains an invalid numeric value")
        if isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            pending.extend(item.values() if isinstance(item, dict) else item)


def read_stored_evidence(row, *, run_id: str, owner_id: str, meeting_id: str) -> dict:
    """Bind the exact typed excerpt to its authoritative row and caller scope."""
    try:
        content = StoredEvidence.model_validate(deepcopy(row.content))
        if (row.run_id, row.owner_id, row.meeting_id) != (run_id, owner_id, meeting_id):
            raise ValueError("Stored evidence row is outside its owning scope")
        if (content.id, content.run_id, content.owner_id, content.sha256) != (row.id, row.run_id, row.owner_id, row.sha256):
            raise ValueError("Stored evidence identity or hash differs from its immutable row")
        return content.model_dump(mode="json", exclude_unset=True)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise UnreadableReport("Stored evidence could not be safely read in its original scope; its record is preserved for review") from None


def verify_report_journal(session, row, run, content, committed):
    """Check journal-backed reports at their saved cutoff, never the live ledger."""
    journal_rows = list(session.scalars(select(Event).where(
        Event.run_id == row.run_id, Event.sequence <= row.event_cutoff,
        Event.event_type == "discussion.obligation_recorded").order_by(Event.sequence)))
    if not journal_rows:
        return  # Pre-journal reports have no durable event history.
    # Intentionally redacted, fail-closed recovery reports contain no issue details.
    if (not content.get("issues") and content.get("readiness") is None
        and content.get("recommendation") is None and not content.get("comparison")
        and content.get("execution_status") in {"failed", "cancelled"}
        and content.get("readiness_dimensions") is None
        and not any(content.get("decision_insights", {}).get(key) for key in ("cost_frontier_option_ids", "dominated_option_ids", "unresolved_option_ids", "sensitivity", "preference_order"))
        and not content.get("calculations") and not content.get("claim_ids")
        and not content.get("essential_dependencies") and not content.get("evidence_assessments")
        and content.get("assessment", {}).get("synthesis_valid") is False
        and content.get("assessment", {}).get("audit_succeeded") is False
        and "Saved contribution and issue details were not assessed for this partial report." in content.get("main_reasons", [])):
        return
    from round_robin.research.discussion import _openings
    journal = {}
    for recorded in journal_rows:
        payload = recorded.payload
        key = (payload["objection_id"], payload["event"]["id"])
        if key in journal:
            raise ValueError
        journal[key] = (recorded.sequence, payload["event"])
    steps = {step.id: step for step in session.scalars(select(Step).where(Step.run_id == row.run_id, Step.id.in_(committed)))}
    if set(steps) != committed:
        raise ValueError
    if any(
        step.status != "succeeded"
        or not isinstance(step.result_json, dict)
        or not isinstance(step.result_json.get("output"), dict)
        for step in steps.values()
    ):
        raise ValueError
    attempt_ids = {step.result_json.get("attempt_id") for step in steps.values()
                   if isinstance(step.result_json, dict) and step.result_json.get("attempt_id")}
    attempts = {attempt.id: attempt for attempt in session.scalars(
        select(Attempt).where(Attempt.run_id == row.run_id, Attempt.id.in_(attempt_ids)))}
    for step in steps.values():
        if step.status != "succeeded" or not isinstance(step.result_json, dict):
            continue
        attempt_id = step.result_json.get("attempt_id")
        if attempt_id:
            attempt = attempts.get(attempt_id)
            if (attempt is None or attempt.step_id != step.id or attempt.provider != step.provider
                or attempt.status != "succeeded" or attempt.response_json != step.result_json
                or attempt.request_json.get("stage") != step.stage
                or attempt.request_json.get("role") != step.role_slot_id):
                raise ValueError
    expected = {}
    for step in steps.values():
        if step.status == "succeeded" and step.result_json:
            for opening in _openings(run, step, step.result_json["output"]):
                if opening["id"] in expected:
                    raise ValueError
                expected[opening["id"]] = (step, opening)
    issues = content.get("issues", [])
    if len({item["id"] for item in issues}) != len(issues) or set(expected) != {item["id"] for item in issues}:
        raise ValueError
    represented = set()
    for issue in issues:
        step, opening = expected[issue["id"]]
        if (issue["source_step_id"], issue["source_provider"], issue["source_stage"]) != (step.id, step.provider, step.stage):
            raise ValueError
        for key in ("concern", "severity", "origin", "target_reference_id", "option_id", "requirement_id", "reference_ids"):
            if issue.get(key, [] if key == "reference_ids" else None) != opening.get(key, [] if key == "reference_ids" else None):
                raise ValueError
        previous_sequence = -1
        for item in issue["events"]:
            key = (issue["id"], item["id"])
            if key in represented:
                raise ValueError
            represented.add(key)
            sequence, event = journal[key]
            actual = steps[item["step_id"]]
            if (item != event or sequence <= previous_sequence or actual.status != "succeeded"
                or (actual.provider, actual.stage) != (item["provider"], item["stage"])):
                raise ValueError
            previous_sequence = sequence
        events = issue["events"]
        status = issue["status"]
        if status == "answered_pending_review" and (not events or events[-1]["disposition"] != "answered"):
            raise ValueError
        if status in {"resolved", "withdrawn"}:
            if not events or events[-1]["disposition"] != status or not events[-1]["reference_ids"]:
                raise ValueError
            final = events[-1]
            if status == "withdrawn" and final["provider"] != issue["source_provider"]:
                raise ValueError
            if status == "resolved" and (final["stage"] != "audit" or not any(
                event["disposition"] == "answered" and event["provider"] != final["provider"]
                and event["input_hash"] == final["input_hash"] for event in events[:-1])):
                raise ValueError
    if represented != set(journal):
        raise ValueError
    responses = defaultdict(Counter)
    for step in steps.values():
        if step.status == "succeeded" and step.result_json:
            for response in step.result_json["output"].get("objection_responses", []):
                key = (step.id, response["objection_id"], response["response"], tuple(response["reference_ids"]))
                responses[key][response["disposition"]] += 1
    represented_responses = defaultdict(Counter)
    for issue in issues:
        for event in issue["events"]:
            if event["disposition"] != "opened":
                key = (event["step_id"], issue["id"], event["response"], tuple(event["reference_ids"]))
                represented_responses[key][event["disposition"]] += 1
    if set(responses) != set(represented_responses):
        raise ValueError
    for key, requested in responses.items():
        actual = represented_responses[key]
        if (sum(actual.values()) != sum(requested.values())
            or set(requested) - {"answered", "resolved", "withdrawn"}
            or set(actual) - {"answered", "resolved", "withdrawn"}
            or actual["resolved"] > requested["resolved"]
            or actual["withdrawn"] > requested["withdrawn"]):
            raise ValueError
    unresolved = any(issue["severity"] in {"critical", "material"} and issue["status"] in {"open", "answered_pending_review"} for issue in issues)
    if unresolved and (content.get("assessment", {}).get("unresolved_critical_defect") is not True
        or content.get("readiness_dimensions") is not None and content["readiness_dimensions"]["material_objections_resolved"]):
        raise ValueError


def read_runtime_report(session, row) -> dict:
    _validate_finite_numbers(row.content)
    try:
        content = deepcopy(row.content)
    except RecursionError:
        raise UnreadableReport("Saved report nesting exceeds supported limits") from None
    if not isinstance(content, dict) or "simulated" not in content:
        raise UnreadableReport("Unsupported historical runtime report; fictional sample schemas require their own reader")
    if any(content.get(key) != getattr(row, key) for key in ("id", "run_id", "version", "event_cutoff")):
        raise UnreadableReport("Saved report identity, version and cutoff must match its immutable record")
    try:
        # Legacy reports omitted this field: recover only bounded identities
        # at the saved cutoff before hydrating any evidence content.
        if "evidence_ids" not in content:
            evidence_ids = list(session.scalars(select(EvidenceRecord.id).where(
                EvidenceRecord.run_id == row.run_id, EvidenceRecord.created_at <= row.created_at
            ).limit(25)))
            content["evidence_ids"] = sorted(evidence_ids)
        evidence_ids = content["evidence_ids"]
        if (not isinstance(evidence_ids, list) or len(evidence_ids) > 24
                or any(not isinstance(identity, str) or len(identity) > 96 for identity in evidence_ids)
                or len(set(evidence_ids)) != len(evidence_ids)):
            raise ValueError("Invalid saved evidence identities")
        evidence = list(session.scalars(select(EvidenceRecord).where(
            EvidenceRecord.run_id == row.run_id, EvidenceRecord.created_at <= row.created_at,
            EvidenceRecord.id.in_(evidence_ids)))) if evidence_ids else []
        committed = set()
        for event in session.scalars(select(Event).where(Event.run_id == row.run_id, Event.sequence <= row.event_cutoff, Event.event_type == "content.committed")):
            if not isinstance(event.payload, dict):
                raise UnreadableReport("Committed evidence scope has an invalid event payload")
            step_id = event.payload.get("step_id")
            if step_id is not None:
                if not isinstance(step_id, str) or not step_id.strip():
                    raise UnreadableReport("Committed contribution identity must be nonblank text")
                committed.add(step_id)
        # Earlier runtime reports did not serialize their identity scope. Rebuild
        # those fields from their original committed cutoff, not later run outputs.
        content.setdefault("contribution_ids", sorted(committed))
        if not set(content["evidence_ids"]) <= {item.id for item in evidence} or not set(content["contribution_ids"]) <= committed:
            raise UnreadableReport("Saved report references were not committed within its run and cutoff")
        run = session.get(Run, row.run_id)
        if run is None:
            raise UnreadableReport("Saved report owning run is unavailable")
        scoped_records = {item.id: item for item in evidence}
        for identity in content["evidence_ids"]:
            read_stored_evidence(scoped_records[identity], run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id)
        from round_robin.database.claim_models import ClaimVersion
        from round_robin.research.reuse import read_claim
        claims = list(session.scalars(select(ClaimVersion).where(ClaimVersion.run_id == row.run_id, ClaimVersion.created_at <= row.created_at)))
        claim_records = {claim.id: claim for claim in claims if claim.step_id is None or claim.step_id in committed}
        content.setdefault("claim_ids", [])
        if not set(content["claim_ids"]) <= set(claim_records):
            raise UnreadableReport("Saved report references claims outside its committed cutoff")
        for identity in content["claim_ids"]:
            read_claim(claim_records[identity], run=run)
        verify_report_journal(session, row, run, content, committed)
        return RuntimeDecisionReport.model_validate(content, context={"run_id": row.run_id, "allowed_evidence_ids": content["evidence_ids"], "allowed_contribution_ids": content["contribution_ids"], "allowed_claim_ids": content["claim_ids"]}).model_dump(mode="json")
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise UnreadableReport("Saved report could not be safely read at its original version and cutoff") from None
