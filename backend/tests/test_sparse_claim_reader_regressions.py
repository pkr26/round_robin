from types import SimpleNamespace

import pytest
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import ClaimRejected, StoredClaim, read_claim


def test_schema_valid_historical_assumption_without_reference_ids_is_readable():
    content = {"id": "claim_valid", "version": 1, "proposition": "A synthetic assumption", "kind": "assumption", "evidence_status": "not_checked", "time_sensitive": False, "verified_at": "2026-01-02T12:00:00Z"}
    assert StoredClaim.model_validate(content).reference_ids == []
    row = SimpleNamespace(id=content["id"], version=1, run_id="run_test", meeting_id="meeting_test", owner_id="owner_test", content=content, sha256=canonical_hash(content))
    run = SimpleNamespace(id="run_test", meeting_id="meeting_test", owner_id="owner_test")
    assert read_claim(row, run=run) == {**content, "sha256": row.sha256}


@pytest.mark.parametrize("bad", [None, "foreign_reference", 3, {}, ["claim_valid"]])
def test_explicit_malformed_references_and_self_reference_still_reject(bad):
    content = {"id": "claim_valid", "version": 1, "proposition": "A synthetic assumption", "kind": "assumption", "evidence_status": "not_checked", "time_sensitive": False, "verified_at": "2026-01-02T12:00:00Z", "reference_ids": bad}
    row = SimpleNamespace(id=content["id"], version=1, run_id="run_test", meeting_id="meeting_test", owner_id="owner_test", content=content, sha256=canonical_hash(content))
    run = SimpleNamespace(id="run_test", meeting_id="meeting_test", owner_id="owner_test")
    with pytest.raises(ClaimRejected):
        read_claim(row, run=run)
