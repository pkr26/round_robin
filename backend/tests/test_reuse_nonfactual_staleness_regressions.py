import pytest
from fastapi.testclient import TestClient
from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import EvidenceRecord, Run
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import claim_packet
from sqlalchemy import select
from test_exhaustive_evidence_reuse import preflight, start

pytest_plugins = ("test_exhaustive_evidence_reuse",)


@pytest.mark.parametrize("kind", ["assumption", "preference", "recommendation"])
@pytest.mark.parametrize("stale", [False, True])
def test_nonfactual_claim_follows_selected_evidence_freshness(parent, kind, stale):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        claim = session.get(ClaimVersion, selections[1]["id"])
        claim.content = {**claim.content, "kind": kind, "time_sensitive": False}
        claim.sha256 = canonical_hash(claim.content)
        selections[1]["sha256"] = claim.sha256
        evidence = session.get(EvidenceRecord, selections[0]["id"])
        evidence.content = {**evidence.content, "time_sensitive": stale}
    with TestClient(app) as client:
        quote = preflight(client, parent, selections)
        assert quote.status_code == 200
        result, _ = start(client, parent, quote.json())
        assert result.status_code == 202
        child_id = result.json()["id"]
    with app.state.session_factory() as session:
        item = claim_packet(session, session.get(Run, child_id))[0]
        assert item["evidence_status"] == ("not_checked" if stale else "supported")


@pytest.mark.parametrize("scenario,expected", [
    ("one_fresh_basis", "supported"),
    ("all_stale_bases", "not_checked"),
    ("no_references", "supported"),
    ("nonfactual_premise", "not_checked"),
    ("stale_factual_premise", "not_checked"),
])
def test_nonfactual_support_preserves_scope_and_transitive_freshness(parent, scenario, expected):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        original = session.get(ClaimVersion, selections[1]["id"])
        evidence = session.get(EvidenceRecord, selections[0]["id"])
        original.content = {**original.content, "time_sensitive": False}
        if scenario == "nonfactual_premise":
            original.content = {**original.content, "kind": "assumption"}
            evidence.content = {**evidence.content, "time_sensitive": False}
        original.sha256 = canonical_hash(original.content)
        selections[1]["sha256"] = original.sha256
        if scenario in {"one_fresh_basis", "all_stale_bases"}:
            fresh = {**evidence.content, "id": "evidence_alternative", "time_sensitive": scenario == "all_stale_bases"}
            session.add(EvidenceRecord(id=fresh["id"], run_id=evidence.run_id, meeting_id=evidence.meeting_id,
                owner_id=evidence.owner_id, sha256=evidence.sha256, content=fresh))
            selections.append({"id": fresh["id"], "kind": "evidence", "version": 1, "sha256": fresh["sha256"]})
            references = []
            bases = [{"reference_ids": [evidence.id]}, {"reference_ids": [fresh["id"]]}]
        else:
            references = [] if scenario == "no_references" else [original.id]
            bases = []
        content = {**original.content, "id": "claim_nonfactual", "kind": "recommendation", "reference_ids": references,
            "support_bases": bases, "time_sensitive": False}
        digest = canonical_hash(content)
        session.add(ClaimVersion(id=content["id"], version=1, run_id=original.run_id, meeting_id=original.meeting_id,
            owner_id=original.owner_id, step_id=original.step_id, sha256=digest, content=content))
        selections.append({"id": content["id"], "kind": "claim", "version": 1, "sha256": digest})
    with TestClient(app) as client:
        quote = preflight(client, parent, selections)
        assert quote.status_code == 200, quote.text
        result, _ = start(client, parent, quote.json())
        assert result.status_code == 202, result.text
        child_id = result.json()["id"]
    with app.state.session_factory() as session:
        mapping = {row.parent_id: row.child_id for row in session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child_id))}
        claims = {item["id"]: item for item in claim_packet(session, session.get(Run, child_id))}
        assert claims[mapping["claim_nonfactual"]]["evidence_status"] == expected
        assert claims[mapping["claim_nonfactual"]]["kind"] == "recommendation"
        if scenario == "nonfactual_premise":
            assert claims[mapping[selections[1]["id"]]]["evidence_status"] == "supported"


def test_nonfactual_claim_cannot_ground_a_supported_factual_claim(parent):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        original = session.get(ClaimVersion, selections[1]["id"])
        original.content = {**original.content, "kind": "assumption", "time_sensitive": False}
        original.sha256 = canonical_hash(original.content)
        selections[1]["sha256"] = original.sha256
        content = {**original.content, "id": "claim_ungrounded_fact", "kind": "fact", "reference_ids": [original.id]}
        digest = canonical_hash(content)
        session.add(ClaimVersion(id=content["id"], version=1, run_id=original.run_id, meeting_id=original.meeting_id,
            owner_id=original.owner_id, step_id=original.step_id, sha256=digest, content=content))
        selections.append({"id": content["id"], "kind": "claim", "version": 1, "sha256": digest})
    with TestClient(app) as client:
        quote = preflight(client, parent, selections)
        assert quote.status_code == 409
