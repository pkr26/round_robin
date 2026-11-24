from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import EvidenceRecord, Run
from round_robin.database.services import canonical_hash
from round_robin.research.claims import ClaimRejected
from round_robin.research.reuse import claim_packet, read_claim
from round_robin.worker.tasks import build_request
from sqlalchemy import select
from test_exhaustive_evidence_reuse import preflight, start

pytest_plugins = ("test_exhaustive_evidence_reuse",)


@pytest.mark.parametrize("kind", ["fact", "calculation", "assumption", "preference", "recommendation"])
def test_sparse_fresh_basis_preserves_supported_and_exact_parent_provenance(parent, kind):
    app, _, run, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        evidence = session.get(EvidenceRecord, selections[0]["id"])
        evidence.content = {**evidence.content, "time_sensitive": False}
        row = session.get(ClaimVersion, selections[1]["id"])
        content = deepcopy(row.content)
        content.pop("reference_ids")
        content.update(kind=kind, evidence_status="supported", time_sensitive=False,
                       support_bases=[{"reference_ids": [evidence.id]}])
        row.content, row.sha256 = content, canonical_hash(content)
        parent_hash = row.sha256
        selected_claim = {**selections[1], "sha256": parent_hash}
    with TestClient(app) as client:
        result = client.get(f"/api/runs/{run['id']}/claims")
        assert result.status_code == 200, result.text
        assert result.json()["items"][0] == {**content, "sha256": parent_hash}
        quote = preflight(client, parent, [selections[0], selected_claim])
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
        assert response.status_code == 202, response.text
        child_id = response.json()["id"]
    with app.state.session_factory() as session:
        child = session.get(Run, child_id)
        copied = claim_packet(session, child, imported_only=True)[0]
        entries = {item.parent_id: item for item in session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child_id))}
        assert copied["evidence_status"] == "supported"
        assert copied["reference_ids"] == []
        assert copied["support_bases"] == [{"reference_ids": [entries[evidence.id].child_id]}]
        assert entries[row.id].parent_sha256 == parent_hash
        assert session.get(ClaimVersion, row.id).content == content
        assert session.get(ClaimVersion, row.id).sha256 == parent_hash
        step = SimpleNamespace(id="step_test", stage="proposal", role_slot_id="proposal_test", calls=0, error_code=None)
        assert build_request(session, child, step).claims == (copied,)


@pytest.mark.parametrize("kind", ["assumption", "preference", "recommendation"])
def test_sparse_unreferenced_nonfactual_is_not_a_grounding_root(parent, kind):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        evidence = session.get(EvidenceRecord, selections[0]["id"])
        evidence.content = {**evidence.content, "time_sensitive": False}
        original = session.get(ClaimVersion, selections[1]["id"])
        sparse = deepcopy(original.content)
        sparse.pop("reference_ids")
        sparse.pop("support_bases", None)
        sparse.update(kind=kind, evidence_status="supported", time_sensitive=False)
        original.content, original.sha256 = sparse, canonical_hash(sparse)
        other = {**sparse, "id": "claim_dependent", "kind": "fact", "reference_ids": [original.id, evidence.id]}
        session.add(ClaimVersion(id=other["id"], version=1, run_id=original.run_id, meeting_id=original.meeting_id,
                                 owner_id=original.owner_id, step_id=original.step_id, content=other, sha256=canonical_hash(other)))
        selected = [{**selections[1], "sha256": original.sha256},
                    {"kind": "claim", "id": other["id"], "version": 1, "sha256": canonical_hash(other)}]
    with TestClient(app) as client:
        quote = preflight(client, parent, selected)
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
        assert response.status_code == 202, response.text
        child_id = response.json()["id"]
    with app.state.session_factory() as session:
        records = claim_packet(session, session.get(Run, child_id), imported_only=True)
        assert {item["kind"]: item["evidence_status"] for item in records} == {kind: "supported", "fact": "not_checked"}
        ids = {entry.parent_id: entry.child_id for entry in session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child_id))}
        dependent = next(item for item in records if item["kind"] == "fact")
        assert dependent["reference_ids"] == [ids[selections[1]["id"]]]


@pytest.mark.parametrize("damage", ["hash", "id", "owner", "null", "self_basis"])
def test_sparse_corruption_still_rejects(parent, damage):
    app, _, run, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        row = session.get(ClaimVersion, selections[1]["id"])
        content = deepcopy(row.content)
        content.pop("reference_ids")
        content.update(kind="assumption", evidence_status="not_checked")
        if damage == "id": content["id"] = "claim_wrong"
        if damage == "null": content["reference_ids"] = None
        if damage == "self_basis": content["support_bases"] = [{"reference_ids": [row.id]}]
        row.content, row.sha256 = content, canonical_hash(content)
        if damage == "hash": row.sha256 = "0" * 64
        if damage == "owner": row.owner_id = "owner_wrong"
        current = session.get(Run, run["id"])
        with pytest.raises(ClaimRejected):
            claim_packet(session, current) if damage == "self_basis" else read_claim(row, run=current)
