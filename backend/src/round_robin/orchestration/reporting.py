"""Saved-record report construction and deterministic comparison policy."""

from datetime import date
from decimal import Decimal
import hashlib
from zoneinfo import ZoneInfo

from round_robin.database.models import utcnow
from round_robin.reports.readiness import derive_readiness
from round_robin.reports.insights import comparison_blocker_note, decision_insights
from round_robin.reports.questions import STANDARD_COST_KINDS, rank_question_worklist
from round_robin.reports.tradeoffs import explain_tradeoff
from round_robin.research.discussion import current_assessments, discussion_issues, expired_quote_refs
from round_robin.research.calculator import CalculationError, lifecycle_total
from round_robin.schemas.briefs import DraftBrief
from round_robin.schemas.contributions import ConstraintResult
from round_robin.schemas.reports import EssentialDependency, OptionComparison, ReviewCoverage
from round_robin.schemas.evidence import Calculation
from round_robin.schemas.runtime import RuntimeDecisionReport, RuntimeOptionComparison
from .budget import aware


def _display(value, maximum):
    """Bound a derived display excerpt; full source stays in saved inputs."""
    return value if len(value) <= maximum else value[:maximum - 1] + "…"


def _derived_id(prefix, original):
    # A single encoding prevents a short source alias from impersonating the
    # digest assigned to a longer alias in the same report.
    return prefix + hashlib.sha256(original.encode()).hexdigest()


def comparison_as_of(run, *, at=None):
    """Resolve an explicit observation time or the historical run creation time."""
    instant = at if at is not None else run.created_at
    if instant is None:
        return None
    brief = run.configuration.get("resolved_brief", run.input_snapshot)
    return aware(instant).astimezone(ZoneInfo(brief.get("timezone", "America/Phoenix"))).date()


def comparison_data(brief: DraftBrief, *, as_of: date | None = None):
    comparisons, calculations = [], []
    for option in brief.options:
        if brief.horizon_months is None:
            continue
        try:
            calculation = lifecycle_total(option, brief.horizon_months, brief.currency, calculation_id=_derived_id("calc_", option.id), as_of=as_of)
            missing = list(calculation.missing_components)
            # Summing entered lines cannot prove omitted applicable charges zero.
            categories = {component.kind for component in option.cost_components}
            for category in STANDARD_COST_KINDS:
                if category not in categories:
                    missing.append(f"Confirm whether {category.replace('_', ' ')} applies; no explicit component or not-applicable entry exists.")
            if missing:
                calculation = calculation.model_copy(update={"output": None, "missing_components": missing})
        except (CalculationError, ValueError):
            calculation = Calculation(id=_derived_id("calc_", option.id), label=f"{_display(option.label, 469)}: unavailable cost calculation", operation="lifecycle_total", inputs=[], formula="Unavailable: incompatible or incomplete monetary inputs", output=None, unit=brief.currency, currency=brief.currency, horizon_months=brief.horizon_months, rounding="No arithmetic result was established", missing_components=["Cost calculation unavailable; check units and currencies."], known_subtotal=None)
            calculations.append(calculation.model_dump(mode="json"))
            comparisons.append({"option_id": option.id, "label": option.label, "role": option.role, "total": None, "known_subtotal": None, "missing_components": calculation.missing_components, "currency": brief.currency, "horizon_months": brief.horizon_months, "constraint_results": [], "calculation_id": calculation.id, "caveat": "No checked comparable total exists."})
            continue
        calculations.append(calculation.model_dump(mode="json"))
        constraints = []
        for requirement in brief.hard_requirements:
            result = "unknown"
            reason = "This requirement needs supplied evidence or human verification; simulation cannot establish it."
            refs = []
            if requirement.metric == "total_cost" and calculation.output is not None and requirement.threshold is not None:
                left, right = Decimal(calculation.output), Decimal(requirement.threshold)
                passes = {"lte": left <= right, "gte": left >= right, "eq": left == right}.get(requirement.operator)
                if passes is not None:
                    result = "pass" if passes else "fail"
                    reason = f"Checked {brief.horizon_months}-month cost {left} {brief.currency} {requirement.operator} {right} {brief.currency}."
                    refs = [calculation.id]
            constraints.append(ConstraintResult(requirement_id=requirement.id, result=result, reason=reason, basis_refs=refs, method="deterministic_calculation" if refs else "fixture_assessment").model_dump(mode="json"))
        comparisons.append(OptionComparison(option_id=option.id, label=option.label, role=option.role, total=calculation.output, known_subtotal=calculation.known_subtotal, missing_components=calculation.missing_components, currency=brief.currency, horizon_months=brief.horizon_months, constraint_results=[ConstraintResult.model_validate(item) for item in constraints], calculation_id=calculation.id, caveat="Entered costs are arithmetic inputs, not independently verified supplier facts.").model_dump(mode="json"))
    return comparisons, calculations


def build_invalid_brief_report(run, steps, *, status, stop_reason, report_id, version, usage, evidence, parent_report=None, parent_report_id=None, parent_report_unavailable=False, parent_brief_unavailable=False, invalid_brief=True, invalid_discussion=False, published_at=None):
    """Publish a partial record without interpreting corrupt saved inputs or obligations."""
    simulated = usage["simulated"]
    # Use UTC for a partial report whose underlying brief cannot be interpreted.
    zone = "UTC" if invalid_brief else DraftBrief.model_validate(run.configuration.get("resolved_brief", run.input_snapshot)).timezone
    succeeded = [step for step in steps if step.status == "succeeded" and step.result_json]
    saved_issues = run.configuration.get("discussion_issues", [])
    if isinstance(saved_issues, dict):
        saved_issues = list(saved_issues.values())
    if not isinstance(saved_issues, list):
        saved_issues = []
    unresolved = any(isinstance(item, dict) and item.get("severity") in {"material", "critical"}
                     and item.get("status") in {"open", "answered_pending_review"} for item in saved_issues)
    unresolved = invalid_discussion or unresolved or any(step.stage == "audit" and bool(step.result_json.get("output", {}).get("critical_defects")) for step in succeeded)
    if invalid_brief and invalid_discussion:
        reason = "The saved decision inputs and discussion ledger cannot be validated, so no comparison or readiness assessment is available."
    elif invalid_discussion:
        reason = "The saved discussion ledger cannot be validated, so no comparison or readiness assessment is available. Unreviewed obligations are treated as unresolved."
    else:
        reason = "The saved decision inputs cannot be validated, so no comparison or readiness assessment is available."
    main_reasons = [reason, "Saved contribution and issue details were not assessed for this partial report."]
    if run.configuration.get("comparison_unavailable_reason") == "evidence_integrity_failed":
        main_reasons.append("Stored evidence could not be validated and is unavailable in this report.")
    body = {
        "id": report_id, "run_id": run.id, "version": version, "event_cutoff": run.event_sequence + 1,
        "valuation_date": (published_at or utcnow()).astimezone(ZoneInfo(zone)).date(), "valuation_timezone": zone,
        "fictional": simulated, "simulated": simulated, "parent_report_id": parent_report_id or (parent_report.get("id") if parent_report else None),
        "execution_status": status, "recommendation": None, "readiness": None,
        "readiness_reasons": [reason], "readiness_unavailable_reason": reason,
        "main_reasons": main_reasons,
        "tradeoff": "The saved inputs could not be interpreted reliably.",
        "largest_uncertainty": reason, "what_would_change_answer": "Review the saved inputs and start a new valid run.",
        "next_action": "Review the saved inputs and start a new valid run.",
        "comparison": [], "calculations": [], "hard_requirement_ids": [],
        "evidence_ids": sorted(item["id"] for item in evidence), "contribution_ids": [step.id for step in succeeded],
        "claim_ids": [], "essential_dependencies": [], "dissent": [], "next_evidence": [],
        "coverage": [{"provider": step.provider, "role": step.stage, "status": "simulated" if simulated and step.status == "succeeded" else step.status,
                      "notes": step.error_code or "Actual saved stage coverage."} for step in steps],
        "assessment": {"synthesis_valid": False, "audit_succeeded": False, "unresolved_critical_defect": unresolved},
        "usage": usage, "stop_reason": stop_reason, "change_summary": (["The prior report is unavailable; its recommendation and readiness were not compared."] if parent_report_unavailable else []) + (["The prior decision inputs are unavailable; input changes were not compared."] if parent_brief_unavailable else []),
        "research_summary": run.configuration.get("research_summary", {"status": "not_requested", "queries": 0, "pages": 0}),
    }
    return RuntimeDecisionReport.model_validate(body, context={"run_id": run.id, "allowed_evidence_ids": set(body["evidence_ids"]),
        "allowed_contribution_ids": set(body["contribution_ids"]), "expected_simulated": simulated,
        "parent_report_id": body["parent_report_id"]}).model_dump(mode="json")


def build_report(run, steps, *, status: str, stop_reason: str, report_id: str, version: int, usage: dict, evidence=None, claims=None, parent_report=None, parent_report_id=None, parent_brief=None, parent_report_unavailable=False, parent_brief_unavailable=False, published_at=None):
    saved_brief = run.configuration.get("resolved_brief", run.input_snapshot)
    brief = DraftBrief.model_validate(saved_brief)
    published_at = published_at or utcnow()
    as_of = published_at.astimezone(ZoneInfo(brief.timezone)).date()
    comparisons, calculations = comparison_data(brief, as_of=as_of) if run.configuration.get("evidence_prepared") and not run.configuration.get("comparison_unavailable_reason") else ([], [])
    succeeded = [step for step in steps if step.status == "succeeded" and step.result_json]
    syntheses = [step for step in succeeded if step.stage == "synthesis"]
    audits = [step for step in succeeded if step.stage == "audit"]
    synthesis = syntheses[-1] if syntheses else None
    audit = audits[-1] if audits else None
    proposals = [step for step in succeeded if step.stage == "proposal"]
    assessment = ReviewCoverage(proposal_providers=list({step.provider for step in proposals}), synthesizer=synthesis.provider if synthesis else None, auditor=audit.provider if audit else None, synthesis_valid=synthesis is not None, audit_succeeded=bool(audit and audit.result_json["output"].get("audit_passed") is True), unresolved_critical_defect=bool(audit and audit.result_json["output"].get("critical_defects")))
    simulated = run.configuration.get("mode", "simulated") == "simulated"
    scoped_evidence = {item["id"]: item for item in (evidence or [])}
    expired_refs = expired_quote_refs(saved_brief, as_of)
    current_refs = scoped_evidence.keys() - expired_refs
    accepted_assessments = current_assessments(run, scoped_evidence, now=published_at) if run.configuration.get("evidence_assessments") else []
    evidence_expiries = [component.quote_valid_until for option in brief.options for component in option.cost_components
                         if component.quote_valid_until is not None]
    evidence_expiries.extend(date.fromisoformat(receipt["valid_until"]) for receipt in accepted_assessments if receipt["valid_until"])
    issues = discussion_issues(run, succeeded)
    unresolved = [item for item in issues if item["status"] in {"open", "answered_pending_review"} and item["severity"] in {"material", "critical"}]
    assessment.unresolved_critical_defect = assessment.unresolved_critical_defect or bool(unresolved)
    cost_requirement_ids = {item.id for item in brief.hard_requirements if item.metric == "total_cost"}
    if not simulated and synthesis:
        synthesis_assessments = {(item["option_id"], item["requirement_id"]): item for item in synthesis.result_json["output"].get("constraints", [])}
        audit_assessments = {(item["option_id"], item["requirement_id"]): item for item in audit.result_json["output"].get("constraints", [])} if audit else {}
        for option in comparisons:
            for index, result in enumerate(option["constraint_results"]):
                if result["method"] == "deterministic_calculation" or result["requirement_id"] in cost_requirement_ids:
                    continue
                key = (option["option_id"], result["requirement_id"])
                proposed = synthesis_assessments.get(key)
                checked = audit_assessments.get(key)
                # A mixed set of premises cannot establish a verdict once one
                # of its quoted terms expires. Keep the original in its step.
                if checked and expired_refs.intersection(checked.get("basis_refs", [])):
                    checked = None
                if proposed and proposed.get("basis_refs") and set(proposed["basis_refs"]) <= current_refs:
                    replacement = {**proposed, "method": "model", "assessor": f"{synthesis.provider} synthesis; inspect the cited user/source passage"}
                    replacement.pop("option_id", None)
                    if checked and checked["result"] != proposed["result"]:
                        replacement["result"] = "unknown"
                        replacement["disputed"] = True
                        replacement["counter_basis_refs"] = list(checked.get("basis_refs", []))
                        replacement["assessor"] = f"{synthesis.provider} synthesis / {audit.provider} audit; unresolved disagreement"
                        replacement["reason"] = "Synthesis and independent audit disagree; inspect saved contributions. Synthesis: " + _display(proposed["reason"], 200) + " / Audit: " + _display(checked["reason"], 200)
                    replacement["assessment_scope"] = "Attributed model assessment of cited supplied passages; not independent fact certification."
                    option["constraint_results"][index] = ConstraintResult.model_validate(replacement).model_dump(mode="json")
                elif checked and checked["result"] == "fail" and checked.get("basis_refs") and set(checked["basis_refs"]) <= current_refs:
                    # A missing synthesis assessment cannot erase a supported
                    # hard-requirement failure found by the independent audit.
                    replacement = {**checked, "method": "model", "assessor": f"{audit.provider} independent audit; inspect the cited user/source passage"}
                    replacement.pop("option_id", None)
                    option["constraint_results"][index] = ConstraintResult.model_validate(replacement).model_dump(mode="json")
    for attestation in accepted_assessments:
        if attestation["target_type"] != "requirement":
            continue
        option = next((item for item in comparisons if item["option_id"] == attestation["option_id"]), None)
        if option is None:
            continue
        for index, result in enumerate(option["constraint_results"]):
            if result["requirement_id"] != attestation["requirement_id"] or result["method"] == "deterministic_calculation":
                continue
            conflicting = bool(result.get("disputed")) or (result["result"] in {"pass", "fail", "not_applicable"} and result["result"] != attestation["result"] and bool(result["basis_refs"]))
            option["constraint_results"][index] = ConstraintResult(requirement_id=result["requirement_id"],
                result="unknown" if conflicting else attestation["result"], basis_refs=attestation["reference_ids"],
                counter_basis_refs=list(dict.fromkeys([*result["basis_refs"], *result.get("counter_basis_refs", [])])) if conflicting else [], disputed=conflicting,
                method="human", assessor="Explicit owner attestation", assessment_scope=attestation["scope"],
                reason="Owner attestation conflicts with an existing assessed basis; resolve the discrepancy." if conflicting else _display(attestation["attestation"], 500)).model_dump(mode="json")
    dependencies = []
    proposed_choice = synthesis.result_json["output"].get("recommendation_option_id") if synthesis else None
    relevant = comparisons
    for item in relevant:
        refs = [item["calculation_id"]] if item["calculation_id"] in {calculation["id"] for calculation in calculations} else []
        cost_sources = list(dict.fromkeys(component.source_ref for option in brief.options if option.id == item["option_id"] for component in option.cost_components))
        attested = next((record for record in accepted_assessments if record["target_type"] == "option_costs" and record["option_id"] == item["option_id"] and record["result"] == "supported"), None)
        supported = bool(not simulated and item["total"] is not None and cost_sources and attested and set(cost_sources) <= set(attested["reference_ids"]))
        dependencies.append(EssentialDependency(id=_derived_id("dependency_", item['option_id']), description=f"All applicable cost components for {_display(item['label'], 430)} must be supplied and current.", status="supported" if supported else "unverified", accepted_scoped_support=supported, reference_ids=refs + cost_sources[:20], origin="user_supplied"))
    selected_for_support = next((item for item in comparisons if item["option_id"] == proposed_choice), None)
    if selected_for_support:
        for requirement in brief.hard_requirements:
            if requirement.metric == "total_cost":
                continue
            receipt = next((item for item in accepted_assessments if item["target_type"] == "requirement" and item["option_id"] == proposed_choice and item["requirement_id"] == requirement.id and item["result"] in {"pass", "not_applicable"}), None)
            verdict = next((item for item in selected_for_support["constraint_results"] if item["requirement_id"] == requirement.id), None)
            accepted = bool(receipt and verdict and not verdict.get("disputed") and verdict["result"] == receipt["result"])
            dependencies.append(EssentialDependency(id=_derived_id("dependency_", f"{proposed_choice}:{requirement.id}"),
                description=f"Scoped acceptance is required for {_display(requirement.description, 400)}; model agreement alone is not verification.",
                status="supported" if accepted else "unverified", accepted_scoped_support=accepted,
                reference_ids=receipt["reference_ids"] if receipt else [], origin="user_supplied"))
    if simulated:
        dependencies.append(EssentialDependency(id="dependency_actual_review", description="Simulated advisers provide no independent provider review or verification.", status="unverified", accepted_scoped_support=False, reference_ids=[], origin="user_supplied"))
    chosen = synthesis.result_json["output"].get("recommendation_option_id") if synthesis else None
    insights = decision_insights(brief, comparisons)
    if simulated and chosen:
        if insights["cost_comparison_blocker"] or insights["cost_frontier_option_ids"] != [chosen]:
            chosen = None
    # Reject any proposed choice with a demonstrably failed hard requirement.
    selected = next((item for item in comparisons if item["option_id"] == chosen), None)
    if expired_refs or selected is None or any(result["result"] == "fail" for result in selected["constraint_results"]):
        chosen = None
    readiness, reasons = derive_readiness(selected_option_id=chosen, comparison=[RuntimeOptionComparison.model_validate(item) for item in comparisons], dependencies=dependencies, coverage=assessment, required_requirement_ids={requirement.id for requirement in brief.hard_requirements}) if comparisons else (None, ["A common horizon, faithfully bounded saved evidence, or sufficient confirmed options is unavailable."])
    if synthesis and simulated and insights["tied_lowest_cost_option_ids"]:
        reasons = ["Multiple feasible options share the lowest comparable total. The simulation has no basis for choosing between them."]
    recommendation = {"option_id": chosen, "option_label": selected["label"], "summary": synthesis.result_json["output"]["summary"], "conditions": reasons if readiness != "ready_to_choose" else []} if synthesis and chosen else None
    dissent = [item["concern"] for item in issues if item["status"] in {"open", "answered_pending_review"} and item["severity"] in {"critical", "material"}]
    for step in reversed(succeeded):
        dissent.extend(step.result_json["output"].get("critical_defects", []))
        dissent.extend(step.result_json["output"].get("dissent", []))
    dissent = list(dict.fromkeys(dissent))
    worklist = rank_question_worklist(brief, comparisons, insights, as_of=as_of, chosen=chosen)
    narrative, scenario_calculations = explain_tradeoff(
        brief, comparisons, insights, as_of=as_of, chosen=chosen,
        question=worklist.lead_question, lead_option_id=worklist.lead_option_id,
        lead_cost_gap=worklist.lead_cost_gap, lead_peer_id=worklist.lead_peer_id,
        viable_cost_ranges=worklist.viable_cost_ranges, quoted_cost_ranges=worklist.quoted_cost_ranges,
    )
    changes = []
    if parent_brief is not None:
        for key, label in (("budget", "Budget"), ("deadline", "Deadline"), ("horizon_months", "Comparison horizon"), ("options", "Options and quoted cost inputs"), ("hard_requirements", "Hard requirements"), ("user_facts", "User-supplied facts"), ("selected_attachment_ids", "Selected evidence")):
            if parent_brief.get(key) != run.input_snapshot.get(key):
                changes.append(f"{label} changed; dependent conclusions were recomputed for this run.")
        changes.append("Historical time-sensitive evidence is not treated as current merely because it appeared in the parent run.")
    if parent_report:
        previous_choice = (parent_report.get("recommendation") or {}).get("option_id")
        if previous_choice != chosen:
            changes.append("The preferred-option result changed from the parent's saved report.")
        if parent_report.get("readiness") != readiness:
            changes.append(f"Decision readiness changed from {parent_report.get('readiness')} to {readiness}.")
    if parent_report_unavailable:
        changes.append("The prior report is unavailable; its recommendation and readiness were not compared.")
    if parent_brief_unavailable:
        changes.append("The prior decision inputs are unavailable; input changes were not compared.")
    main_reasons = ["Comparable cost uses bounded Decimal calculations from this run's supplied inputs." if calculations
                    else "Comparable cost remains unavailable until the missing confirmed inputs can be represented faithfully."]
    if insights["tied_lowest_cost_option_ids"]:
        labels = sorted(item["label"] for item in comparisons if item["option_id"] in insights["tied_lowest_cost_option_ids"])
        main_reasons.append("Tied for the lowest comparable total: " + ", ".join(labels) + ".")
    if note := comparison_blocker_note(insights, comparisons):
        main_reasons.append(note)
    if run.configuration.get("comparison_unavailable_reason") == "evidence_integrity_failed":
        main_reasons.append("Stored evidence could not be validated and is unavailable in this report.")
    body = {
        "schema_version": "1.0", "id": report_id, "run_id": run.id, "version": version,
        "valuation_date": as_of, "valuation_timezone": brief.timezone, "evidence_valid_through": min(evidence_expiries, default=None),
        "event_cutoff": run.event_sequence + 1, "fictional": simulated, "simulated": simulated, "parent_report_id": parent_report_id or (parent_report.get("id") if parent_report else None),
        "execution_status": status, "recommendation": recommendation, "readiness": readiness,
        "readiness_reasons": reasons, "readiness_unavailable_reason": reasons[0] if readiness is None else None,
        "main_reasons": main_reasons,
        **narrative, "largest_uncertainty": reasons[0],
        "comparison": comparisons, "calculations": calculations + scenario_calculations, "hard_requirement_ids": [requirement.id for requirement in brief.hard_requirements],
        "evidence_ids": sorted(scoped_evidence), "contribution_ids": [step.id for step in succeeded], "claim_ids": sorted(claim["id"] for claim in (claims or [])),
        "essential_dependencies": [dependency.model_dump(mode="json") for dependency in dependencies],
        "dissent": dissent[:20], "dissent_overflow_count": max(0, len(dissent) - 20), "issues": issues,
        "evidence_assessments": accepted_assessments, "algorithm_policy_version": "comparable_cost_choices_v2",
        "readiness_dimensions": {"complete_comparison": bool(comparisons) and all(item["total"] is not None and not item["missing_components"] for item in comparisons),
            "checked_arithmetic": bool(calculations) and all(item["output"] is not None for item in calculations),
            "scoped_cost_support": bool(comparisons) and all(item.accepted_scoped_support for item in dependencies if item.id in {_derived_id("dependency_", option["option_id"]) for option in comparisons}),
            "requirements_assessed": bool(selected) and {item["requirement_id"] for item in selected["constraint_results"] if item["result"] in {"pass", "not_applicable"} and not item.get("disputed")} == {item.id for item in brief.hard_requirements} and all(item.accepted_scoped_support for item in dependencies if item.id not in {_derived_id("dependency_", option["option_id"]) for option in comparisons} | {"dependency_actual_review"}),
            "distinct_review": assessment.audit_succeeded and bool(assessment.auditor and assessment.synthesizer and assessment.auditor != assessment.synthesizer),
            "material_objections_resolved": not unresolved, "basis_scope": "Exact supplied inputs and explicit owner attestations; model review is attributed and is not independent factual certification."},
        "decision_insights": insights,
        "next_evidence_count": len(worklist.requests) if comparisons else None,
        "next_evidence": worklist.requests[:20], "coverage": [{"provider": step.provider, "role": step.stage, "status": "simulated" if simulated and step.status == "succeeded" else step.status, "notes": step.error_code or "Actual saved stage coverage."} for step in steps],
        "assessment": assessment.model_dump(mode="json"), "usage": usage, "stop_reason": stop_reason, "change_summary": changes[:20],
        "research_summary": run.configuration.get("research_summary", {"status": "not_requested", "queries": 0, "pages": 0}),
    }
    return RuntimeDecisionReport.model_validate(body, context={"run_id": run.id, "allowed_evidence_ids": set(scoped_evidence), "allowed_contribution_ids": {step.id for step in succeeded}, "expected_simulated": simulated, "hard_requirement_ids": {requirement.id for requirement in brief.hard_requirements}, "allowed_option_ids": {option.id for option in brief.options}, "parent_report_id": body["parent_report_id"]}).model_dump(mode="json")
