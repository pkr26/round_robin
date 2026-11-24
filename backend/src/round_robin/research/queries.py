"""Search receives only frozen user-approved public terms, never model prose."""

from dataclasses import dataclass
import hashlib
import json


class QueryRejected(ValueError):
    pass


TEMPLATES = {
    "comparison_v1": "{terms} comparison {region}",
    "costs_v1": "{terms} all-inclusive installation maintenance costs {region}",
    "counterevidence_v1": "{terms} limitations exclusions reliability {region}",
}


@dataclass(frozen=True)
class PublicScope:
    terms: tuple[tuple[str, str], ...] = ()
    region: str = ""
    template_ids: tuple[str, ...] = tuple(TEMPLATES)

    def __post_init__(self):
        if len(self.terms) > 8 or len(self.region) > 120 or len({key for key, _ in self.terms}) != len(self.terms):
            raise QueryRejected("Public scope exceeds its declared bounds")
        if any(not key or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value) for key, value in self.terms):
            raise QueryRejected("Public terms must be explicitly approved bounded text")
        if any(item not in TEMPLATES for item in self.template_ids):
            raise QueryRejected("Unknown public query template")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps({"terms": self.terms, "region": self.region, "templates": self.template_ids}, sort_keys=True).encode()).hexdigest()


def render_query(scope: PublicScope, *, term_ids: list[str], template_id: str) -> str:
    if template_id not in scope.template_ids or not 1 <= len(term_ids) <= 8 or len(set(term_ids)) != len(term_ids):
        raise QueryRejected("A search request must select approved term and template identities")
    approved = dict(scope.terms)
    if any(identity not in approved for identity in term_ids):
        raise QueryRejected("Unapproved term identity; private prose is never a search fallback")
    return TEMPLATES[template_id].format(terms=" ".join(approved[identity] for identity in term_ids), region=scope.region).strip()
