from copy import deepcopy

import pytest
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import Run, Step
from round_robin.database.services import canonical_hash
from round_robin.research.claims import ClaimRejected
from round_robin.research.reuse import persist_claims, read_claim
from sqlalchemy import func, select

pytest_plugins = ("test_exhaustive_evidence_reuse",)


def test_real_persistence_rewrites_scoped_claims_and_rejects_foreign_nested_refs(parent):
    app, _, run, _, selections, _, _, _ = parent
    source = selections[0]["id"]
    original = {"claims": [
        {"alias": "new_claim_1", "proposition": "First synthetic claim", "kind": "fact", "evidence_status": "supported", "reference_ids": [source]},
        {"alias": "new_claim_2", "proposition": "Second synthetic claim", "kind": "fact", "evidence_status": "supported", "reference_ids": ["new_claim_1"]},
    ], "constraints": [{"basis_refs": ["new_claim_2", source], "counter_basis_refs": []}]}
    before = deepcopy(original)
    with app.state.session_factory.begin() as session:
        current = session.get(Run, run["id"])
        step = Step(id="step_new_claims", run_id=current.id, stage="critique", role_slot_id="critic_test", provider="google", status="succeeded")
        session.add(step)
        session.flush()
        committed = persist_claims(session, current, step, original, {source})
        first, second = committed["claims"]
        assert original == before
        assert first["id"] != second["id"]
        assert first["reference_ids"] == [source]
        assert second["reference_ids"] == [first["id"]]
        assert committed["constraints"][0]["basis_refs"] == [second["id"], source]
        for item in (first, second):
            row = session.get(ClaimVersion, item["id"])
            assert read_claim(row, run=current) == item
            assert row.sha256 == canonical_hash(row.content)

        bad = {"claims": [{"alias": "new_claim_1", "proposition": "Forged", "kind": "fact", "evidence_status": "not_checked", "reference_ids": []}],
               "constraints": [{"basis_refs": ["source_foreign"], "counter_basis_refs": []}]}
        next_step = Step(id="step_rejected_claim", run_id=current.id, stage="revision", role_slot_id="revision_test", provider="google", status="succeeded")
        session.add(next_step)
        session.flush()
        count = session.scalar(select(func.count()).select_from(ClaimVersion).where(ClaimVersion.run_id == current.id))
        with pytest.raises(ClaimRejected):
            persist_claims(session, current, next_step, bad, {source})
        session.flush()
        assert session.scalar(select(func.count()).select_from(ClaimVersion).where(ClaimVersion.run_id == current.id)) == count
        assert list(session.scalars(select(ClaimVersion).where(ClaimVersion.step_id == next_step.id))) == []
