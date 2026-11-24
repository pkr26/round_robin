from copy import deepcopy
from types import SimpleNamespace

import pytest
from round_robin.database.services import canonical_hash
from round_robin.research.claims import ClaimRejected
from round_robin.research.reuse import read_claim


@pytest.fixture
def pair():
    content = {"id": "claim_test", "version": 1, "proposition": "A synthetic claim", "kind": "fact", "evidence_status": "supported", "reference_ids": ["evidence_test"], "time_sensitive": False, "verified_at": "2026-01-02T12:00:00Z"}
    row = SimpleNamespace(id=content["id"], version=1, run_id="run_test", meeting_id="meeting_test", owner_id="owner_test", content=content, sha256=canonical_hash(content))
    return row, SimpleNamespace(id="run_test", meeting_id="meeting_test", owner_id="owner_test")


def test_historical_explicit_reference_claim_preserves_shape_and_hash(pair):
    row, run = pair
    original = deepcopy(row.content)
    result = read_claim(row, run=run)
    assert result == {**original, "sha256": row.sha256}
    assert "support_bases" not in result and row.content == original


@pytest.mark.parametrize("field", ["run_id", "meeting_id", "owner_id", "id", "version", "sha256"])
def test_mismatched_authoritative_scope_or_identity_rejects_without_mutation(pair, field):
    row, run = pair
    original = deepcopy(row.content)
    setattr(row, field, 2 if field == "version" else "other_value")
    with pytest.raises(ClaimRejected):
        read_claim(row, run=run)
    assert row.content == original


@pytest.mark.parametrize("kind", ["fact", "calculation"])
def test_supported_factual_claim_needs_reference_or_explicit_basis(pair, kind):
    row, run = pair
    row.content = {**row.content, "kind": kind, "reference_ids": []}
    row.sha256 = canonical_hash(row.content)
    with pytest.raises(ClaimRejected):
        read_claim(row, run=run)
    row.content = {**row.content, "support_bases": [{"reference_ids": ["evidence_test"]}]}
    row.sha256 = canonical_hash(row.content)
    assert read_claim(row, run=run)["support_bases"] == [{"reference_ids": ["evidence_test"]}]


def test_self_reference_rejected_and_nonfactual_unverified_claim_can_be_read(pair):
    row, run = pair
    row.content = {**row.content, "kind": "assumption", "evidence_status": "not_checked", "reference_ids": [row.id]}
    row.sha256 = canonical_hash(row.content)
    with pytest.raises(ClaimRejected):
        read_claim(row, run=run)
    row.content = {**row.content, "reference_ids": []}
    row.sha256 = canonical_hash(row.content)
    assert read_claim(row, run=run)["evidence_status"] == "not_checked"
