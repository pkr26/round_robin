"""Validate scoped aliases before atomically persisting server-issued claim IDs."""

from copy import deepcopy
import re
import uuid


class ClaimRejected(ValueError):
    pass


def grounded_claim_aliases(claims: list[dict], *, support_reference_ids: set[str], prior_claims=(), reference_statuses=None) -> set[str]:
    """Reject circular citations and find claims with a source/calculation path.

    An adviser assertion is not a source merely because its ID is in scope.
    Resolve every edge, including those after a genuine source reference, so a
    source cannot disguise a separate self-reference or cycle.
    """
    records = {claim["id"]: claim for claim in prior_claims}
    records.update({claim["alias"]: claim for claim in claims})
    prior_ids = {claim["id"] for claim in prior_claims}
    graph = {identity: list(dict.fromkeys([*record.get("reference_ids", []),
        *record.get("refuting_reference_ids", []),
        *(reference for basis in record.get("support_bases", []) for reference in basis["reference_ids"])]))
        for identity, record in records.items()}
    statuses = reference_statuses or {}
    active, resolved = set(), {}

    def rooted(identity):
        if identity in support_reference_ids and identity not in graph:
            return statuses.get(identity) not in {"contradicted", "mixed", "not_checked", "stale", "fictional"}
        if identity in active:
            raise ClaimRejected("Claims cannot cite themselves or form a circular support chain")
        if identity in resolved:
            return resolved[identity]
        if identity not in graph:
            return False
        active.add(identity)
        # Visit every edge before reducing support, including refutation edges,
        # so an accepted source cannot hide a circular justification.
        roots = {reference: rooted(reference) for reference in graph[identity]}
        active.remove(identity)
        record = records[identity]
        bases = record.get("support_bases", [])
        positive = any(all(roots.get(reference, False) for reference in basis["reference_ids"]) for basis in bases) if bases else any(roots.get(reference, False) for reference in record.get("reference_ids", []))
        # A refuting edge is an unresolved counter-basis, not positive support.
        # Prior assertions must themselves have qualified support to transmit it.
        resolved[identity] = bool(positive and record.get("kind", "fact") in {"fact", "calculation"} and not record.get("refuting_reference_ids") and
            record.get("evidence_status", "supported") == "supported")
        return resolved[identity]

    return {claim["alias"] for claim in claims if rooted(claim["alias"])}


def assign_claim_ids(contribution: dict, *, allowed_reference_ids: set[str], id_factory=None) -> tuple[dict, dict[str, str]]:
    factory = id_factory or (lambda: "claim_" + uuid.uuid4().hex)
    if not isinstance(contribution, dict) or not isinstance(contribution.get("claims", []), list):
        raise ClaimRejected("Claims require a typed contribution")
    claims = contribution.get("claims", [])
    if any(not isinstance(claim, dict) for claim in claims):
        raise ClaimRejected("Claims require typed claim records")
    aliases = [claim.get("alias") for claim in claims]
    if len(aliases) > 20 or any(not isinstance(alias, str) or not re.fullmatch(r"new_claim_[1-9][0-9]*", alias) for alias in aliases) or len(set(aliases)) != len(aliases):
        raise ClaimRejected("Claims require unique response-local aliases")
    if any(not isinstance(claim.get("reference_ids", []), list) or any(not isinstance(reference, str) for reference in claim.get("reference_ids", [])) for claim in claims):
        raise ClaimRejected("Reference collections must be typed arrays")
    grounded_claim_aliases(claims, support_reference_ids=allowed_reference_ids)
    mapping = {alias: factory() for alias in aliases}
    if len(set(mapping.values())) != len(mapping) or set(mapping.values()) & allowed_reference_ids:
        raise ClaimRejected("Persistent claim identities must be new and unique")
    result = deepcopy(contribution)
    references = set()

    def rewrite(value):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key in {"reference_ids", "basis_refs", "counter_basis_refs", "refuting_reference_ids"}:
                    if not isinstance(item, list):
                        raise ClaimRejected("Reference collections must be typed arrays")
                    for identity in item:
                        if not isinstance(identity, str) or identity not in allowed_reference_ids and identity not in mapping:
                            raise ClaimRejected("Unknown or cross-run authoritative reference")
                    references.update(item)
                    value[key] = [mapping.get(identity, identity) for identity in item]
                elif key == "target_reference_id":
                    if item is None:
                        continue
                    if not isinstance(item, str) or item not in allowed_reference_ids and item not in mapping:
                        raise ClaimRejected("Objection target is not scoped to this run")
                    value[key] = mapping.get(item, item)
                else:
                    rewrite(item)
        elif isinstance(value, list):
            for item in value:
                rewrite(item)

    rewrite(result)
    if len(references) > 40:
        raise ClaimRejected("Contribution exceeds forty existing references")
    for claim in result.get("claims", []):
        claim["id"] = mapping[claim["alias"]]
    return result, mapping


def validate_reuse(*, selected_items: list[dict], parent_items: dict[str, dict], meeting_id: str, child_run_id: str) -> list[dict]:
    """Child membership is explicit; time-sensitive parent support is stale until rechecked."""
    if len(selected_items) > 24 or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in selected_items) or len({item["id"] for item in selected_items}) != len(selected_items):
        raise ClaimRejected("Reuse requires unique bounded selections")
    result = []
    for selected in selected_items:
        parent = parent_items.get(selected.get("id"))
        if not parent or parent.get("meeting_id") != meeting_id or parent.get("content_hash") != selected.get("content_hash") or type(selected.get("version")) is not int or selected["version"] < 1 or parent.get("version") != selected["version"]:
            raise ClaimRejected("Reuse must identify an approved immutable parent item in the same meeting")
        result.append({"id": "evidence_" + uuid.uuid4().hex, "run_id": child_run_id, "origin_id": selected["id"], "origin_version": parent["version"], "content_hash": parent["content_hash"], "verified_at": parent.get("verified_at"), "verification_status": "not_checked" if parent.get("time_sensitive") else parent.get("verification_status", "not_checked")})
    return result
