"""Application readiness policy; this does not certify real-world correctness."""

from round_robin.schemas.reports import EssentialDependency, OptionComparison, Readiness, ReviewCoverage


def derive_readiness(*, selected_option_id: str | None, comparison: list[OptionComparison], dependencies: list[EssentialDependency], coverage: ReviewCoverage, required_requirement_ids: set[str]) -> tuple[Readiness | None, list[str]]:
    if not coverage.synthesis_valid:
        return None, ["No valid synthesis exists; recommendation and readiness are unavailable."]
    if comparison and all(any(result.result == "fail" and result.basis_refs and not result.disputed and result.requirement_id in required_requirement_ids for result in option.constraint_results) for option in comparison):
        return "no_feasible_option", ["Every considered option fails a hard requirement within its stated evidence or supplied-input scope; this is not independent supplier certification."]
    selected = next((option for option in comparison if option.option_id == selected_option_id), None)
    if selected is None or any(result.result == "fail" for result in selected.constraint_results):
        return "insufficient_evidence", ["No defensible preferred option with a feasibility basis has been established."]
    reasons = []
    if coverage.unresolved_critical_defect:
        reasons.append("The audit retains an unresolved critical defect.")
    if not coverage.audit_succeeded or not coverage.auditor or not coverage.synthesizer or coverage.auditor == coverage.synthesizer:
        reasons.append("A successful auditor distinct from the synthesizer is required.")
    if len(set(coverage.proposal_providers)) < 2:
        reasons.append("Fewer than two distinct validated proposals are available.")
    assessed_requirements = {result.requirement_id for result in selected.constraint_results}
    if required_requirement_ids - assessed_requirements:
        reasons.append("Required hard-requirement assessments are missing.")
    for result in selected.constraint_results:
        if result.result == "unknown" or result.disputed:
            reasons.append(f"A selected-option hard requirement remains unknown: {result.reason}")
        elif result.requirement_id in required_requirement_ids and not result.basis_refs:
            reasons.append(f"A selected-option hard requirement lacks a supporting reference: {result.reason}")
    for dependency in dependencies:
        if dependency.status != "supported" or not dependency.accepted_scoped_support or not dependency.reference_ids:
            reasons.append(f"Essential dependency remains {dependency.status}: {dependency.description}")
    if reasons:
        return "conditional", reasons
    return "ready_to_choose", ["The selected option passes applicable requirements under the stated evidence or owner-attestation scope, with accepted dependencies and a distinct audit. This does not certify real-world truth."]
