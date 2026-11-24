"""Substantive case identities shared by study and release validators.

Only explicit identity metadata and known unordered record collections are
normalized. Narrative text, numbers, evidence and ordered sequences stay exact.
This catches aliases, not semantic plagiarism or independence between projects.
"""
import hashlib
import json


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


_REFERENCE_KEYS = {"source_ref", "reference_ids", "basis_refs", "predecessor_id", "origin_id"}
_UNORDERED_RECORDS = {"options", "hard_requirements", "cost_components", "checked_snapshots"}


def substantive_fingerprint(brief, evidence=None):
    # Brief titles/revision numbers are presentation/version metadata. No other
    # arbitrary 'title' field is discarded (source titles can be meaningful).
    if isinstance(brief, dict):
        brief = {key: value for key, value in brief.items() if key not in {"id", "title", "revision"}}
    value = {"brief": brief, "checked_snapshots": evidence or []}
    aliases = {}

    def intrinsic(item):
        if isinstance(item, dict):
            return {key: intrinsic(child) for key, child in item.items() if key != "id" and key not in _REFERENCE_KEYS}
        if isinstance(item, list):
            return [intrinsic(child) for child in item]
        return item

    def collect(item):
        if isinstance(item, dict):
            if isinstance(item.get("id"), str):
                aliases[item["id"]] = "identity_" + _hash(intrinsic(item))
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
    collect(value)

    def normalize(item, key=None):
        if isinstance(item, dict):
            return {name: normalize(child, name) for name, child in item.items() if name != "id"}
        if isinstance(item, list):
            children = [normalize(child, key) for child in item]
            return sorted(children, key=_hash) if key in _UNORDERED_RECORDS else children
        if isinstance(item, str) and key in _REFERENCE_KEYS:
            return aliases.get(item, item)
        return item
    return _hash(normalize(value))


def reject_development_overlap(fingerprints, development):
    if not isinstance(development, list) or any(not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in development):
        raise ValueError("Development case fingerprints must be SHA-256 identities")
    if set(fingerprints) & set(development):
        raise ValueError("Substantive development cases cannot enter held-out coverage under new aliases")
