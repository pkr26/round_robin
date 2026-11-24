import pytest
from fastapi.testclient import TestClient
from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import EvidenceRecord, Run
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import claim_packet
from sqlalchemy import select
from test_exhaustive_evidence_reuse import preflight, start

pytest_plugins = ("test_exhaustive_evidence_reuse",)


@pytest.mark.parametrize("scenario,expected", [
    ("alternative", "supported"),
    ("conjunction", "not_checked"),
    ("all_bases_lost", "not_checked"),
    ("generic_omitted", "not_checked"),
    ("selected_generic", "supported"),
    ("selected_stale", "not_checked"),
    ("claim_time_sensitive", "not_checked"),
    ("omitted_refuting", "not_checked"),
    ("selected_refuting", "not_checked"),
    ("not_checked", "not_checked"),
    ("fictional", "fictional"),
    ("kind_assumption", "not_checked"),
    ("kind_preference", "not_checked"),
    ("kind_recommendation", "not_checked"),
    ("kind_assumption_fresh", "supported"),
    ("kind_preference_fresh", "supported"),
    ("kind_recommendation_fresh", "supported"),
    ("nonfact_generic", "not_checked"),
    ("nonfact_counter", "not_checked"),
    ("nonfact_selected_counter", "not_checked"),
    ("nonfact_lost_bases", "not_checked"),
])
def test_selected_alternatives_preserve_only_proven_support(parent, scenario, expected):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        original = session.get(ClaimVersion, selections[1]["id"])
        omitted = session.get(EvidenceRecord, selections[0]["id"])
        fresh = {**omitted.content, "id": "evidence_selected", "time_sensitive": scenario == "selected_stale" or scenario.startswith("kind_") and not scenario.endswith("_fresh")}
        session.add(EvidenceRecord(id=fresh["id"], run_id=omitted.run_id, meeting_id=omitted.meeting_id,
            owner_id=omitted.owner_id, sha256=omitted.sha256, content=fresh))
        selected = [{"id": fresh["id"], "kind": "evidence", "version": 1, "sha256": fresh["sha256"]}]
        bases = [{"reference_ids": [omitted.id]}, {"reference_ids": [fresh["id"]]}]
        if scenario == "conjunction":
            bases = [{"reference_ids": [omitted.id, fresh["id"]]}]
        if scenario in {"all_bases_lost", "nonfact_lost_bases"}:
            bases = [{"reference_ids": [omitted.id]}]
        generic = [omitted.id] if scenario in {"generic_omitted", "nonfact_generic"} else ([fresh["id"]] if scenario in {"selected_generic", "all_bases_lost", "nonfact_lost_bases"} else [])
        refuting = [omitted.id] if scenario in {"omitted_refuting", "nonfact_counter"} else ([fresh["id"]] if scenario in {"selected_refuting", "nonfact_selected_counter"} else [])
        status = "mixed" if refuting and not scenario.startswith("nonfact_") else scenario if scenario in {"not_checked", "fictional"} else "supported"
        content = {**original.content, "id": "claim_alternatives", "time_sensitive": scenario == "claim_time_sensitive",
                   "reference_ids": generic, "support_bases": bases, "refuting_reference_ids": refuting, "evidence_status": status,
                   "kind": scenario.removeprefix("kind_").removesuffix("_fresh") if scenario.startswith("kind_") else "assumption" if scenario.startswith("nonfact_") else "fact"}
        digest = canonical_hash(content)
        session.add(ClaimVersion(id=content["id"], version=1, run_id=original.run_id, meeting_id=original.meeting_id,
            owner_id=original.owner_id, step_id=original.step_id, sha256=digest, content=content))
        selected.append({"id": content["id"], "kind": "claim", "version": 1, "sha256": digest})
    with TestClient(app) as client:
        quote = preflight(client, parent, selected)
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
        assert response.status_code == 202, response.text
        child_id = response.json()["id"]
    with app.state.session_factory() as session:
        records = list(session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child_id)))
        mapping = {row.parent_id: row.child_id for row in records}
        result = claim_packet(session, session.get(Run, child_id))[0]
        assert result["id"] == mapping["claim_alternatives"]
        assert result["evidence_status"] == expected
        if scenario in {"alternative", "selected_generic"}:
            assert result["support_bases"] == [{"reference_ids": [mapping["evidence_selected"]]}]
        if scenario == "selected_refuting":
            assert result["refuting_reference_ids"] == [mapping["evidence_selected"]]
        assert session.get(ClaimVersion, "claim_alternatives").sha256 == digest
        assert next(row for row in records if row.kind == "claim").parent_sha256 == digest
