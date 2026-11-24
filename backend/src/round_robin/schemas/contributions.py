from typing import Literal, Self

from pydantic import Field, model_validator

from .common import Contract, Id, ShortText

Provider = Literal["openai", "anthropic", "google"]


class ConstraintResult(Contract):
    requirement_id: Id
    result: Literal["pass", "fail", "unknown", "not_applicable"]
    basis_refs: list[Id] = Field(default_factory=list, max_length=40)
    method: Literal["deterministic_calculation", "fixture_assessment", "human", "model"]
    assessor: ShortText = "offline fixture"
    reason: ShortText
    counter_basis_refs: list[Id] = Field(default_factory=list, max_length=40)
    assessment_scope: ShortText | None = None
    disputed: bool = False


class ClaimSupportBasis(Contract):
    """All references in one basis are required; bases are alternatives."""
    reference_ids: list[Id] = Field(min_length=1, max_length=40)


class Claim(Contract):
    alias: str = Field(pattern=r"^new_claim_[1-9][0-9]*$", max_length=32)
    proposition: ShortText
    kind: Literal["fact", "calculation", "assumption", "preference", "recommendation"]
    evidence_status: Literal["supported", "contradicted", "mixed", "not_checked", "fictional"]
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    predecessor_id: Id | None = None
    support_bases: list[ClaimSupportBasis] = Field(default_factory=list, max_length=8)
    refuting_reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    support_scope: ShortText | None = None


class DiscussionObjection(Contract):
    alias: str = Field(pattern=r"^new_objection_[1-9][0-9]*$", max_length=40)
    concern: ShortText
    severity: Literal["minor", "material", "critical"]
    target_reference_id: Id | None = None
    option_id: Id | None = None
    requirement_id: Id | None = None
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)


class ObjectionResponse(Contract):
    objection_id: Id
    disposition: Literal["answered", "resolved", "withdrawn"]
    response: ShortText
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)


class Objection(Contract):
    target_reference_id: Id
    concern: ShortText
    material: bool
    disposition: Literal["accepted", "qualified", "rejected", "unresolved"]


class Contribution(Contract):
    id: Id
    provider: Provider
    model: ShortText
    role: ShortText
    stage: Literal["intake", "proposal", "critique", "revision", "synthesis", "audit"]
    fictional: Literal[True] = True
    summary: str = Field(min_length=1, max_length=4000)
    public_rationale: str = Field(min_length=1, max_length=12000)
    claims: list[Claim] = Field(default_factory=list, max_length=20)
    alternatives: list[ShortText] = Field(default_factory=list, max_length=5)
    objections: list[Objection] = Field(default_factory=list, max_length=12)
    revision_responses: list[ShortText] = Field(default_factory=list, max_length=12)
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    constraint_results: list[ConstraintResult] = Field(default_factory=list, max_length=20)
    assumptions: list[ShortText] = Field(default_factory=list, max_length=20)
    open_questions: list[ShortText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def unique_aliases_and_bounded_references(self) -> Self:
        if len({claim.alias for claim in self.claims}) != len(self.claims):
            raise ValueError("New claim aliases must be unique within a response")
        references = set(self.reference_ids)
        for claim in self.claims:
            references.update(claim.reference_ids)
        if len(references) > 40:
            raise ValueError("At most forty existing references per response are supported")
        return self
