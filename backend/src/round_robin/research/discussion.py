"""Immutable attributed objection events and deterministic scoped assessments."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib

from sqlalchemy import select

from round_robin.database.models import Attempt, Event, EvidenceRecord, Step, utcnow
from round_robin.database.services import append_event, canonical_hash
from round_robin.reports.read_adapters import read_stored_evidence
from round_robin.schemas.discussion import DiscussionIssue, ScopedAssessment
from .claims import ClaimRejected

MAX_ISSUES = 864  # 24 bounded stage outputs, at most 36 opening observations each.


def input_hash(run):
    return canonical_hash(run.configuration.get("resolved_brief", run.input_snapshot))


def _issue_id(run_id, step_id, origin, alias):
    return "objection_" + hashlib.sha256(f"{run_id}:{step_id}:{origin}:{alias}".encode()).hexdigest()[:48]


def _openings(run, step, output):
    for value in output.get("objections", []):
        yield {**value, "id": value.get("id") or _issue_id(run.id, step.id, "structured", value["alias"]), "origin": "structured"}
    for origin, severity, values in (("critical_defect", "critical", output.get("critical_defects", [])), ("dissent", "minor", output.get("dissent", []))):
        for index, concern in enumerate(dict.fromkeys(values)):
            yield {"id": _issue_id(run.id, step.id, origin, str(index)), "concern": concern, "severity": severity, "origin": origin}


def saved_discussion_issues(run):
    """Validate the saved ledger without silently discarding any identities."""
    saved = run.configuration.get("discussion_issues", [])
    if isinstance(saved, dict):
        saved = saved.values()
    elif not isinstance(saved, list):
        raise ClaimRejected("Invalid saved discussion ledger")
    result = {}
    try:
        for item in saved:
            issue = DiscussionIssue.model_validate(item).model_dump(mode="json")
            if issue["id"] in result:
                raise ClaimRejected("Duplicate saved discussion identity")
            result[issue["id"]] = issue
    except (TypeError, ValueError):
        raise ClaimRejected("Invalid saved discussion ledger") from None
    if len(result) > MAX_ISSUES:
        raise ClaimRejected("Discussion obligations exceed the bounded ledger")
    return result


def verify_discussion_provenance(session, run):
    """Bind each saved obligation to its committed contribution and packet."""
    saved = saved_discussion_issues(run)
    try:
        events = list(session.scalars(select(Event).where(Event.run_id == run.id, Event.event_type.in_(["content.committed", "discussion.obligation_recorded"]))))
        committed = set()
        for event in events:
            if event.event_type == "content.committed":
                step_id = event.payload.get("step_id")
                if step_id is not None:
                    if not isinstance(step_id, str) or not step_id.strip():
                        raise ValueError
                    committed.add(step_id)
        journal = {}
        for event in events:
            if event.event_type == "discussion.obligation_recorded":
                payload = event.payload
                key = (payload["objection_id"], payload["event"]["id"])
                if key in journal:
                    raise ValueError
                journal[key] = (event.sequence, payload["event"])
        steps = {step.id: step for step in session.scalars(select(Step).where(Step.run_id == run.id, Step.id.in_(committed)))}
        if set(steps) != committed:
            raise ValueError
        if any(
            step.status != "succeeded"
            or not isinstance(step.result_json, dict)
            or not isinstance(step.result_json.get("output"), dict)
            for step in steps.values()
        ):
            raise ValueError
        attempt_ids = {step.result_json.get("attempt_id") for step in steps.values() if isinstance(step.result_json, dict) and step.result_json.get("attempt_id")}
        attempts = {attempt.id: attempt for attempt in session.scalars(select(Attempt).where(Attempt.run_id == run.id, Attempt.id.in_(attempt_ids)))}
        step_cache = {}
        brief = run.configuration.get("resolved_brief", run.input_snapshot)
        option_ids = {item["id"] for item in brief.get("options", [])}
        requirement_ids = {item["id"] for item in brief.get("hard_requirements", [])}
        needed_references = {reference for issue in saved.values() for reference in issue["reference_ids"]}
        needed_references |= {issue["target_reference_id"] for issue in saved.values() if issue["target_reference_id"] is not None}
        needed_references |= {reference for issue in saved.values() for event in issue["events"] for reference in event["reference_ids"]}
        known_references = set(steps)
        for row in session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id, EvidenceRecord.id.in_(needed_references))):
            read_stored_evidence(row, run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id)
            known_references.add(row.id)
        from round_robin.database.claim_models import ClaimVersion
        from round_robin.research.reuse import read_claim
        claim_records = {}
        for row in session.scalars(select(ClaimVersion).where(ClaimVersion.run_id == run.id, ClaimVersion.id.in_(needed_references))):
            if row.step_id is not None and row.step_id not in committed:
                raise ValueError
            read_claim(row, run=run)
            known_references.add(row.id)
            claim_records[row.id] = row
        if needed_references - known_references:
            from round_robin.orchestration.reporting import comparison_as_of, comparison_data
            from round_robin.schemas.briefs import DraftBrief
            _, calculations = comparison_data(DraftBrief.model_validate(brief), as_of=comparison_as_of(run))
            known_references.update(item["id"] for item in calculations)
        if not needed_references <= known_references:
            raise ValueError

        def step_and_scope(step_id, provider, stage):
            key = (step_id, provider, stage)
            if key in step_cache:
                return step_cache[key]
            step = steps[step_id]
            if step.status != "succeeded" or step.provider != provider or step.stage != stage or not isinstance(step.result_json, dict):
                raise ValueError
            output = step.result_json["output"]
            attempt_id = step.result_json.get("attempt_id")
            attempt = attempts.get(attempt_id) if attempt_id else None
            if attempt_id and attempt is None or attempt is not None and (
                attempt.step_id != step.id or attempt.provider != step.provider or attempt.status != "succeeded"
                or attempt.response_json != step.result_json or attempt.request_json.get("stage") != step.stage
                or attempt.request_json.get("role") != step.role_slot_id
            ):
                raise ValueError
            allowed = set(attempt.request_json["allowed_reference_ids"]) if attempt is not None else set()
            local_claims = {claim["id"] for claim in output.get("claims", []) if isinstance(claim, dict) and claim.get("id") in claim_records and claim_records[claim["id"]].step_id == step.id}
            step_cache[key] = step, output, allowed | local_claims
            return step_cache[key]

        for issue in saved.values():
            step, output, allowed = step_and_scope(issue["source_step_id"], issue["source_provider"], issue["source_stage"])
            opening = next((item for item in _openings(run, step, output) if item["id"] == issue["id"]), None)
            if opening is None:
                raise ValueError
            for key in ("concern", "severity", "origin", "target_reference_id", "option_id", "requirement_id", "reference_ids"):
                if opening.get(key, [] if key == "reference_ids" else None) != issue.get(key):
                    raise ValueError
            references = set(issue["reference_ids"]) | ({issue["target_reference_id"]} if issue["target_reference_id"] is not None else set())
            if not references <= allowed or issue["option_id"] is not None and issue["option_id"] not in option_ids or issue["requirement_id"] is not None and issue["requirement_id"] not in requirement_ids:
                raise ValueError
            previous_sequence = -1
            for item in issue["events"]:
                _, _, event_scope = step_and_scope(item["step_id"], item["provider"], item["stage"])
                recorded = journal.get((issue["id"], item["id"]))
                if (not set(item["reference_ids"]) <= event_scope or recorded is None
                    or recorded[1] != item or recorded[0] <= previous_sequence):
                    raise ValueError
                previous_sequence = recorded[0]
        if journal or run.configuration.get("discussion_policy_version") == "scoped_obligations_v1":
            recorded = set(journal)
            represented = {(issue["id"], event["id"]) for issue in saved.values() for event in issue["events"]}
            if recorded != represented:
                raise ValueError
        expected = {opening["id"] for step in steps.values() if step.status == "succeeded" and step.result_json for opening in _openings(run, step, step.result_json["output"])}
        if expected != set(saved):
            raise ValueError
        if journal or run.configuration.get("discussion_policy_version") == "scoped_obligations_v1":
            responses = defaultdict(Counter)
            for step in steps.values():
                if step.status == "succeeded" and step.result_json:
                    for response in step.result_json["output"].get("objection_responses", []):
                        key = (step.id, response["objection_id"], response["response"], tuple(response["reference_ids"]))
                        responses[key][response["disposition"]] += 1
            represented_responses = defaultdict(Counter)
            for issue in saved.values():
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
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        raise ClaimRejected("Invalid saved discussion provenance") from None


def discussion_issues(run, steps=()):
    """Return a full bounded ledger, including old contributions without a ledger."""
    current_hash = input_hash(run)
    issues = saved_discussion_issues(run)
    for step in steps:
        if step.status != "succeeded" or not step.result_json:
            continue
        for item in _openings(run, step, step.result_json["output"]):
            if item["id"] not in issues:
                issues[item["id"]] = _new_issue(item, step, current_hash)
    if len(issues) > MAX_ISSUES:
        raise ClaimRejected("Discussion obligations exceed the bounded ledger")
    for item in issues.values():
        if item["status"] == "answered_pending_review" and (not item["events"] or item["events"][-1]["disposition"] != "answered"):
            item["status"] = "open"
        if item["status"] not in {"resolved", "withdrawn"}:
            continue
        events = item["events"]
        last = events[-1] if events else None
        verified = bool(last and last["input_hash"] == current_hash and last["disposition"] == item["status"] and last["reference_ids"])
        if verified and item["status"] == "resolved":
            verified = last["stage"] == "audit" and any(
                event["disposition"] == "answered" and event["input_hash"] == current_hash and event["provider"] != last["provider"]
                for event in events[:-1]
            )
        elif verified and item["status"] == "withdrawn":
            verified = last["provider"] == item["source_provider"]
        if not verified:
            # An absent, stale, or unreviewed resolution cannot clear the issue.
            item["status"] = "open"
    return sorted(issues.values(), key=lambda item: ({"critical": 0, "material": 1, "minor": 2}[item["severity"]], item["id"]))


def _new_issue(item, step, digest):
    value = {key: item[key] for key in ("id", "concern", "severity", "origin", "target_reference_id", "option_id", "requirement_id", "reference_ids") if key in item}
    return DiscussionIssue.model_validate({**value, "source_step_id": step.id, "source_provider": step.provider, "source_stage": step.stage,
        "events": [{"id": "issueevent_" + hashlib.sha256(f"{item['id']}:opened".encode()).hexdigest()[:48], "step_id": step.id, "provider": step.provider,
            "stage": step.stage, "disposition": "opened", "response": item["concern"], "reference_ids": item.get("reference_ids", []), "input_hash": digest}]}).model_dump(mode="json")


def open_objection_packet(session, run):
    # No silent truncation: ProviderRequest's serialized context ceiling rejects
    # a packet that cannot faithfully carry its material obligations.
    steps = session.scalars(select(Step).where(Step.run_id == run.id, Step.status == "succeeded"))
    return [item for item in discussion_issues(run, steps) if item["severity"] in {"material", "critical"} and item["status"] in {"open", "answered_pending_review"}]


def reduce_discussion(run, step, output, allowed_reference_ids):
    """Pure contribution reduction shared by durable and fixed-evidence runs."""
    if step.run_id != run.id:
        raise ClaimRejected("Discussion step is outside the current run")
    result = deepcopy(output)
    issues = {item["id"]: item for item in discussion_issues(run)}
    digest = input_hash(run)
    pending_events = []
    local_ids = {claim["id"] for claim in result.get("claims", []) if "id" in claim}
    allowed = set(allowed_reference_ids) | local_ids
    options = {item["id"] for item in run.configuration.get("resolved_brief", run.input_snapshot).get("options", [])}
    requirements = {item["id"] for item in run.configuration.get("resolved_brief", run.input_snapshot).get("hard_requirements", [])}
    for item in _openings(run, step, result):
        if not set(item.get("reference_ids", [])) <= allowed or item.get("target_reference_id") is not None and item["target_reference_id"] not in allowed:
            raise ClaimRejected("Objection references outside the committed step scope")
        if item.get("option_id") is not None and item["option_id"] not in options or item.get("requirement_id") is not None and item["requirement_id"] not in requirements:
            raise ClaimRejected("Objection target is outside the frozen brief")
        if item["id"] not in issues:
            issues[item["id"]] = _new_issue(item, step, digest)
            pending_events.append((item["id"], issues[item["id"]]["events"][0]))
    for response in result.get("objection_responses", []):
        item = issues.get(response["objection_id"])
        if item is None or not set(response["reference_ids"]) <= allowed:
            raise ClaimRejected("Objection response is outside the committed scope")
        disposition = response["disposition"]
        # A model cannot self-certify its answer. A distinct auditor must review
        # a prior answer, cite scoped evidence and explicitly accept it.
        if disposition == "resolved":
            prior_answers = [event for event in item["events"] if event["disposition"] == "answered" and event["input_hash"] == digest and event["provider"] != step.provider]
            if step.stage != "audit" or not prior_answers or not response["reference_ids"]:
                disposition = "answered"
        elif disposition == "withdrawn" and (step.provider != item["source_provider"] or not response["reference_ids"]):
            disposition = "answered"
        event = {"id": "issueevent_" + hashlib.sha256(f"{item['id']}:{step.id}:{len(item['events'])}".encode()).hexdigest()[:48],
            "step_id": step.id, "provider": step.provider, "stage": step.stage, "disposition": disposition,
            "response": response["response"], "reference_ids": response["reference_ids"], "input_hash": digest}
        item["events"].append(event)
        item["status"] = {"answered": "answered_pending_review", "resolved": "resolved", "withdrawn": "withdrawn"}[disposition]
        pending_events.append((item["id"], event))
    if len(issues) > MAX_ISSUES:
        raise ClaimRejected("Discussion obligations exceed the bounded ledger")
    values = [DiscussionIssue.model_validate(item).model_dump(mode="json") for item in issues.values()]
    return result, values, pending_events


def persist_discussion(session, run, step, output, allowed_reference_ids):
    result, values, pending_events = reduce_discussion(run, step, output, allowed_reference_ids)
    run.configuration = {**run.configuration, "discussion_policy_version": "scoped_obligations_v1", "discussion_issues": values}
    for identity, event in pending_events:
        append_event(session, run, "discussion.obligation_recorded", {"objection_id": identity, "event": event})
    return result


def assessment_target(run, target_type, option_id, requirement_id=None):
    brief = run.configuration.get("resolved_brief", run.input_snapshot)
    option = next((item for item in brief.get("options", []) if item["id"] == option_id), None)
    if option is None:
        raise ClaimRejected("Assessment option is outside the frozen brief")
    if target_type == "option_costs":
        if requirement_id is not None:
            raise ClaimRejected("Cost support cannot impersonate a requirement verdict")
        target = {"option": option, "currency": brief["currency"], "horizon_months": brief.get("horizon_months"), "timezone": brief.get("timezone")}
    elif target_type == "requirement":
        requirement = next((item for item in brief.get("hard_requirements", []) if item["id"] == requirement_id), None)
        if requirement is None or requirement["metric"] == "total_cost":
            raise ClaimRejected("Requirement must exist and cannot override deterministic cost arithmetic")
        target = {"option": option, "requirement": requirement}
    else:
        raise ClaimRejected("Unknown assessment target")
    return canonical_hash(target)


def assessment_expiry(brief, target_type, option_id, reference_ids, valid_until):
    """Earliest inclusive date on the attestation or the quote terms it relies on."""
    expiries = [valid_until] if valid_until else []
    refs = set(reference_ids)
    for option in brief.get("options", []):
        covers_option = target_type == "option_costs" and option["id"] == option_id
        for component in option.get("cost_components", []):
            if covers_option or component["source_ref"] in refs:
                if expiry := component.get("quote_valid_until"):
                    expiries.append(expiry)
    return min(expiries, default=None)


def expired_quote_refs(brief, as_of):
    today = as_of.isoformat()
    return {component["source_ref"] for option in brief.get("options", []) for component in option.get("cost_components", [])
            if component.get("quote_valid_until") and component["quote_valid_until"] < today}


def current_packet_sources(brief, evidence, calculations, claims, as_of):
    """Keep historical quote passages inspectable; withhold claims with expired support."""
    from .claims import grounded_claim_aliases
    expired = expired_quote_refs(brief, as_of)
    if not expired:
        return evidence, claims
    evidence = [{**item, "verification_status": "not_checked"} if item["id"] in expired else item for item in evidence]
    statuses = {item["id"]: item.get("verification_status", "not_checked") for item in evidence}
    roots = set(statuses) | {item["id"] for item in calculations if item.get("output") is not None}
    grounded = grounded_claim_aliases([{**item, "alias": item["id"]} for item in claims], support_reference_ids=roots, reference_statuses=statuses)
    claims = [item for item in claims if item["evidence_status"] != "supported" or item["kind"] not in {"fact", "calculation"} or item["id"] in grounded]
    return evidence, claims


def current_assessments(run, evidence, *, now=None):
    now = now or utcnow()
    from zoneinfo import ZoneInfo
    brief = run.configuration.get("resolved_brief", run.input_snapshot)
    today = now.astimezone(ZoneInfo(brief.get("timezone", "America/Phoenix"))).date()
    latest = {}
    for raw in run.configuration.get("evidence_assessments", []):
        record = ScopedAssessment.model_validate(raw).model_dump(mode="json")
        if record["run_id"] != run.id or record["actor_id"] != run.owner_id:
            raise ClaimRejected("Assessment ownership does not match its run")
        key = (record["target_type"], record["option_id"], record["requirement_id"])
        # Latest immutable record supersedes prior decisions, even when expired.
        latest[key] = record
    result = []
    for record in latest.values():
        try:
            target = assessment_target(run, record["target_type"], record["option_id"], record["requirement_id"])
        except ClaimRejected:
            continue
        expires = assessment_expiry(brief, record["target_type"], record["option_id"], record["reference_ids"], record["valid_until"])
        if target != record["target_hash"] or expires and expires < today.isoformat():
            continue
        if any(ref not in evidence or evidence[ref]["sha256"] != digest for ref, digest in record["evidence_hashes"].items()):
            continue
        if set(record["reference_ids"]) != set(record["evidence_hashes"]):
            continue
        result.append(record)
    return result
