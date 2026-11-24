from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import EvidenceRecord, Run
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import claim_packet
from sqlalchemy import select
from test_exhaustive_evidence_reuse import preflight, start

pytest_plugins = ("test_exhaustive_evidence_reuse",)


def test_nontemporal_claim_is_downgraded_when_its_imported_evidence_is_stale(parent):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        claim = session.get(ClaimVersion, selections[1]["id"])
        claim.content = {**claim.content, "time_sensitive": False}
        claim.sha256 = canonical_hash(claim.content)
        selections[1]["sha256"] = claim.sha256
    with TestClient(app) as client:
        response, _ = start(client, parent, preflight(client, parent, selections).json())
        assert response.status_code == 202, response.text
        child_id = response.json()["id"]
    with app.state.session_factory() as session:
        entries = list(session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child_id)))
        ids = {row.parent_id: row.child_id for row in entries}
        assert all(row.parent_sha256 == selections[index]["sha256"] for index, row in enumerate(sorted(entries, key=lambda row: 0 if row.kind == "evidence" else 1)))
        assert session.get(EvidenceRecord, ids[selections[0]["id"]]).content["verification_status"] == "not_checked"
        claims = claim_packet(session, session.get(Run, child_id))
        assert claims[0]["evidence_status"] == "not_checked"
        assert session.get(ClaimVersion, selections[1]["id"]).content["evidence_status"] == "supported"
        assert claims[0]["reference_ids"] == [ids[selections[0]["id"]]]
        from round_robin.worker.tasks import build_request
        step = SimpleNamespace(id="step_synthetic_proposal", stage="proposal", role_slot_id="proposal_synthetic", calls=0, error_code=None)
        packet = build_request(session, session.get(Run, child_id), step)
        assert packet.claims[0]["evidence_status"] == "not_checked"

    # Run the actual orchestration and worker with local simulated providers.
    from test_orchestration import drain
    drain(app.state.session_factory)
    with app.state.session_factory() as session:
        completed = session.get(Run, child_id)
        assert completed.status == "completed"


@pytest.mark.parametrize("basis,expected", [("chain", "not_checked"), ("alternative", "supported"), ("conjunction", "not_checked")])
def test_imported_support_graph_propagates_only_required_staleness(parent, basis, expected):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        original = session.get(ClaimVersion, selections[1]["id"])
        original.content = {**original.content, "time_sensitive": False}
        original.sha256 = canonical_hash(original.content)
        selections[1]["sha256"] = original.sha256
        evidence = session.get(EvidenceRecord, selections[0]["id"])
        fresh = {**evidence.content, "id": "evidence_fresh", "time_sensitive": False}
        session.add(EvidenceRecord(id=fresh["id"], run_id=evidence.run_id, meeting_id=evidence.meeting_id, owner_id=evidence.owner_id, sha256=evidence.sha256, content=fresh))
        selections.append({"id": fresh["id"], "kind": "evidence", "version": 1, "sha256": evidence.sha256})
        edges = [{"reference_ids": [original.id]}] if basis == "chain" else ([{"reference_ids": [evidence.id]}, {"reference_ids": [fresh["id"]]}] if basis == "alternative" else [{"reference_ids": [evidence.id, fresh["id"]]}])
        content = {**original.content, "id": "claim_derived", "proposition": "Derived synthetic claim", "reference_ids": [], "support_bases": edges}
        digest = canonical_hash(content)
        session.add(ClaimVersion(id=content["id"], version=1, run_id=original.run_id, meeting_id=original.meeting_id, owner_id=original.owner_id, step_id=original.step_id, sha256=digest, content=content))
        selections.append({"id": content["id"], "kind": "claim", "version": 1, "sha256": digest})
    with TestClient(app) as client:
        quote = preflight(client, parent, selections)
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
        assert response.status_code == 202, response.text
        child_id = response.json()["id"]
    with app.state.session_factory() as session:
        records = list(session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child_id)))
        mapping = {record.parent_id: record.child_id for record in records}
        imported = {row["id"]: row for row in claim_packet(session, session.get(Run, child_id))}
        assert imported[mapping["claim_derived"]]["evidence_status"] == expected
        assert imported[mapping[selections[1]["id"]]]["evidence_status"] == "not_checked"
        assert len(records) == len(selections)
