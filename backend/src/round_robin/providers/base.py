from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Literal

from pydantic import ConfigDict, Field

from round_robin.schemas.common import Contract, Id, ShortText
from round_robin.schemas.contributions import Claim, DiscussionObjection, ObjectionResponse


PROVIDERS = ("openai", "anthropic", "google")
OUTPUT_TOKENS = {"intake": 2048, "proposal": 3072, "critique": 1536, "evidence_review": 2048, "revision": 2048, "synthesis": 4096, "audit": 2048}
MAX_PACKET_BYTES = 128_000
MAX_INITIAL_PACKET_BYTES = 48_000
MAX_CONTRIBUTION_FOOTPRINT = 8_000


def contribution_footprint(output: dict) -> int:
    """Conservative v2 space for raw content, IDs, claims and issue provenance.

    Seven substantive contributions fit the standard path, including correction.
    The multiplier covers simultaneous peer/claim/obligation representations;
    metadata allowances cover server-issued identities and timestamps.
    """
    raw = len(json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    openings = sum(len(output.get(key, [])) for key in ("objections", "critical_defects", "dissent"))
    return raw * 3 + 640 * (len(output.get("claims", [])) + openings + len(output.get("objection_responses", [])))


class OutputConstraint(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    option_id: Id
    requirement_id: Id
    result: Literal["pass", "fail", "unknown", "not_applicable"]
    reason: str = Field(min_length=1, max_length=500)
    basis_refs: list[Id] = Field(max_length=40)


class IntakeChange(Contract):
    field: Literal["goal", "question", "horizon_months", "hard_requirements", "options"]
    proposed_value_json: str = Field(min_length=1, max_length=4000)
    reason: ShortText
    reference_ids: list[Id] = Field(default_factory=list, max_length=12)


class ProviderOutput(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    summary: str = Field(min_length=1, max_length=4000)
    recommendation_option_id: Id | None
    reference_ids: list[Id] = Field(max_length=40)
    questions: list[ShortText] = Field(max_length=3)
    assumptions: list[ShortText] = Field(max_length=20)
    constraints: list[OutputConstraint] = Field(max_length=100)
    audit_passed: bool | None
    critical_defects: list[ShortText] = Field(max_length=12)
    dissent: list[ShortText] = Field(max_length=12)
    claims: list[Claim] = Field(default_factory=list, max_length=20)
    proposed_changes: list[IntakeChange] = Field(default_factory=list, max_length=5)
    objections: list[DiscussionObjection] = Field(default_factory=list, max_length=12)
    objection_responses: list[ObjectionResponse] = Field(default_factory=list, max_length=40)


@dataclass(frozen=True)
class ProviderRequest:
    run_id: str
    step_id: str
    stage: str
    role_slot: str
    brief: dict
    evidence: list[dict]
    peer_outputs: list[dict]
    allowed_reference_ids: frozenset[str]
    max_output_tokens: int
    timeout_seconds: float
    repair: bool = False
    calculations: tuple[dict, ...] = ()
    claims: tuple[dict, ...] = ()
    cost_as_of: str | None = None
    repair_diagnostics: tuple[dict, ...] = ()
    open_objections: tuple[dict, ...] = ()
    assessments: tuple[dict, ...] = ()

    def packet(self) -> dict:
        if self.stage == "proposal" and self.peer_outputs:
            raise ProviderFailure("independence_violation", retryable=False, may_have_sent=False)
        claim_ids = {item["id"] for item in self.claims}
        peer_packets = []
        for peer in self.peer_outputs:
            value = {**peer, "output": dict(peer["output"])}
            if all(item.get("id") in claim_ids for item in value["output"].get("claims", [])):
                value["output"].pop("claims", None)
            peer_packets.append(value)
        # The full ledger is saved separately. Deliver current obligations and
        # the last answer needed for independent review without replaying all
        # prior opening/response text in every packet.
        obligations = []
        for issue in self.open_objections:
            value = {key: item for key, item in issue.items() if key != "events"}
            answers = [item for item in issue.get("events", []) if item.get("disposition") == "answered"]
            if answers:
                value["latest_answer"] = answers[-1]
            obligations.append(value)
        result = {
            "stage": self.stage, "role": self.role_slot, "brief": self.brief,
            "evidence": self.evidence, "peer_outputs": peer_packets,
            "allowed_reference_ids": sorted(self.allowed_reference_ids),
            "calculations": list(self.calculations),
            "claims": list(self.claims),
            "cost_as_of": self.cost_as_of,
            "repair": self.repair,
            "repair_diagnostics": list(self.repair_diagnostics),
            "open_objections": obligations,
            "assessments": list(self.assessments),
        }
        try:
            raw = json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError) as exc:
            raise ProviderFailure("invalid_packet", retryable=False, may_have_sent=False) from exc
        if len(raw.encode()) > MAX_PACKET_BYTES or len(self.evidence) > 24 or any(len(str(e.get("text", ""))) > 2000 for e in self.evidence):
            raise ProviderFailure("context_limit", retryable=False, may_have_sent=False)
        if self.stage in {"intake", "proposal"} and len(raw.encode()) > MAX_INITIAL_PACKET_BYTES:
            raise ProviderFailure("context_limit", retryable=False, may_have_sent=False)
        if not 1 <= self.max_output_tokens <= OUTPUT_TOKENS.get(self.stage, 4096) or not 0 < self.timeout_seconds <= 90:
            raise ProviderFailure("invalid_request_limits", retryable=False, may_have_sent=False)
        return result


@dataclass(frozen=True)
class ProviderResult:
    output: ProviderOutput
    cost_usd: Decimal | None
    usage: dict
    simulated: bool
    actual_provider: str
    actual_model: str
    provider_request_id: str | None = None
    raw_text: str | None = None


class ProviderFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False, may_have_sent: bool = True, retry_after: float | None = None, cost_usd: Decimal | None = None, provider_request_id: str | None = None, raw_text: str | None = None, diagnostics: tuple[dict, ...] = ()):
        # Exception text is deliberately content/credential free.
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.may_have_sent = may_have_sent
        self.retry_after = retry_after
        self.cost_usd = cost_usd
        self.provider_request_id = provider_request_id
        self.raw_text = raw_text
        self.diagnostics = diagnostics


def validate_output(output: ProviderOutput, request: ProviderRequest) -> ProviderOutput:
    if contribution_footprint(output.model_dump(mode="json")) > MAX_CONTRIBUTION_FOOTPRINT:
        raise ProviderFailure("schema_invalid", retryable=True, diagnostics=({"code": "contribution_context_envelope", "path": "$", "instruction": "Return a concise complete response within the 8,000-byte downstream footprint. Preserve material objections; split no required premise or concern silently."},))
    if request.stage == "intake" and (output.claims or output.objections or output.objection_responses):
        raise ProviderFailure("schema_invalid", retryable=True, diagnostics=({"code": "unconfirmed_intake_content", "path": "claims/objections/objection_responses", "instruction": "Use questions and proposed_changes during intake; do not commit claims or objections until the brief is confirmed."},))
    allowed_options = {o["id"] for o in request.brief.get("options", [])}
    allowed_requirements = {r["id"] for r in request.brief.get("hard_requirements", [])}
    references = set(output.reference_ids)
    if len({change.field for change in output.proposed_changes}) != len(output.proposed_changes):
        raise ProviderFailure("schema_invalid", retryable=True, diagnostics=({"code": "duplicate_intake_field", "path": "proposed_changes"},))
    for change in output.proposed_changes:
        if request.stage != "intake":
            raise ProviderFailure("unexpected_brief_change", retryable=False)
        try:
            value = json.loads(change.proposed_value_json)
            from round_robin.schemas.briefs import DraftBrief
            DraftBrief.model_validate({**request.brief, change.field: value})
        except (ValueError, TypeError):
            raise ProviderFailure("schema_invalid", retryable=True, diagnostics=({"code": "invalid_intake_change", "path": f"proposed_changes.{change.field}"},)) from None
        references.update(change.reference_ids)
    aliases = {claim.alias for claim in output.claims}
    if len(aliases) != len(output.claims):
        raise ProviderFailure("schema_invalid", retryable=True)
    prior_claim_ids = {claim["id"] for claim in request.claims}
    for claim in output.claims:
        if claim.predecessor_id is not None and claim.predecessor_id not in prior_claim_ids:
            raise ProviderFailure("invalid_reference", retryable=True)
        references.update(claim.reference_ids)
        references.update(claim.refuting_reference_ids)
        for basis in claim.support_bases:
            references.update(basis.reference_ids)
    if len({item.alias for item in output.objections}) != len(output.objections):
        raise ProviderFailure("schema_invalid", retryable=True)
    for objection in output.objections:
        if (objection.option_id is not None and objection.option_id not in allowed_options
                or objection.requirement_id is not None and objection.requirement_id not in allowed_requirements):
            raise ProviderFailure("invalid_reference", retryable=True)
        references.update(objection.reference_ids)
        if objection.target_reference_id is not None:
            references.add(objection.target_reference_id)
    open_ids = {item["id"] for item in request.open_objections}
    response_ids = [item.objection_id for item in output.objection_responses]
    if len(response_ids) != len(set(response_ids)) or not set(response_ids) <= open_ids:
        raise ProviderFailure("invalid_reference", retryable=True)
    for response in output.objection_responses:
        references.update(response.reference_ids)
    if len({(result.option_id, result.requirement_id) for result in output.constraints}) != len(output.constraints):
        raise ProviderFailure("schema_invalid", retryable=True)
    for result in output.constraints:
        if result.option_id not in allowed_options or result.requirement_id not in allowed_requirements:
            raise ProviderFailure("invalid_reference", retryable=True)
        references.update(result.basis_refs)
    if len(references) > 40:
        raise ProviderFailure("schema_invalid", retryable=True)
    if not references <= request.allowed_reference_ids | aliases or output.recommendation_option_id is not None and output.recommendation_option_id not in allowed_options:
        raise ProviderFailure("invalid_reference", retryable=True, diagnostics=({"code": "invalid_reference", "path": "reference_ids/constraints/claims", "instruction": "Use only allowed_reference_ids or declared new claim aliases."},))
    from round_robin.research.claims import ClaimRejected, grounded_claim_aliases
    try:
        grounded = grounded_claim_aliases(
            [claim.model_dump(mode="json") for claim in output.claims],
            support_reference_ids={item["id"] for item in request.evidence} | {item["id"] for item in request.calculations if item.get("output") is not None},
            prior_claims=request.claims,
            reference_statuses={item["id"]: item.get("verification_status", "not_checked") for item in request.evidence},
        )
    except ClaimRejected as exc:
        raise ProviderFailure("invalid_reference", retryable=True) from exc
    for claim in output.claims:
        if claim.kind in {"fact", "calculation"} and claim.evidence_status == "supported" and claim.alias not in grounded:
            claim.evidence_status = "not_checked"
    if request.stage != "intake" and output.questions:
        raise ProviderFailure("unexpected_clarification", retryable=False)
    if request.stage in {"proposal", "synthesis"} and not output.constraints and not output.reference_ids and output.recommendation_option_id is None:
        raise ProviderFailure("substantive_content_unavailable", retryable=False)
    if request.stage == "audit" and output.audit_passed is None:
        raise ProviderFailure("missing_audit_result", retryable=True)
    return output
