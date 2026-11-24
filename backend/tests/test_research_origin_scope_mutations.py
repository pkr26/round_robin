"""Retrieved duplicate text retains its same-run evidence origin, not another run's."""
import hashlib
from datetime import timedelta

from round_robin.database.models import (
    EvidenceRecord,
    Meeting,
    Owner,
    PreflightRecord,
    Run,
    utcnow,
)
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse
from round_robin.research.search import SearchHit
from sqlalchemy import select

pytest_plugins = ("test_research_service",)


def evidence(run, identity, origin_group, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    content = {"id": identity, "run_id": run.id, "owner_id": run.owner_id, "source_title": "Synthetic source",
               "text": text, "sha256": digest, "origin": "user_supplied", "verification_status": "user_supplied",
               "verified_at": utcnow().isoformat(), "time_sensitive": True, "origin_group": origin_group,
               "locator": f"extracted_text[0:{len(text)}]", "scope": "Synthetic origin grouping fixture"}
    return EvidenceRecord(id=identity, run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id,
                          sha256=digest, content=content)


def test_retrieved_duplicate_retains_local_origin_and_excludes_foreign_evidence(research_run, monkeypatch):
    text = "Synthetic source"
    with research_run.begin() as session:
        local = session.get(Run, "run_local")
        assert service.schedule_research(session, local)
        session.add(Owner(id="owner_foreign")); session.flush()
        session.add(Meeting(id="meeting_foreign", owner_id="owner_foreign", title="Foreign")); session.flush()
        session.add(PreflightRecord(id="preflight_foreign", meeting_id="meeting_foreign", owner_id="owner_foreign",
                                   draft_revision=1, draft_hash="a" * 64, configuration_hash="b" * 64,
                                   payload={}, expires_at=utcnow() + timedelta(minutes=10)))
        session.flush()
        foreign = Run(id="run_foreign", meeting_id="meeting_foreign", owner_id="owner_foreign",
                      preflight_id="preflight_foreign", status="completed", input_snapshot={}, configuration={},
                      initial_admission_expires_at=utcnow() + timedelta(minutes=10))
        session.add(foreign); session.flush()
        session.add_all([evidence(local, "evidence_local", "origin_local", text),
                         evidence(foreign, "evidence_foreign", "origin_foreign", text)])

    packet_inputs = []
    original_packet_bytes = service.proposal_packet_bytes
    def capture(session, run, passages):
        packet_inputs.append([item["id"] for item in passages])
        return original_packet_bytes(session, run, passages)
    monkeypatch.setattr(service, "proposal_packet_bytes", capture)

    class Search:
        def search(self, query, **kwargs):
            return [SearchHit("Synthetic", "https://example.com/quote")]
    class Transport:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, text.encode())
    assert service.execute_research("run_local", session_factory=research_run, search=Search(),
                                    transport=Transport(), resolver=lambda _: ["93.184.216.34"])
    assert packet_inputs[0] == ["evidence_local"]
    assert all("evidence_foreign" not in ids for ids in packet_inputs)
    with research_run() as session:
        rows = list(session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == "run_local")))
        retrieved = [row for row in rows if row.content["origin"] == "retrieved"]
        assert len(retrieved) == 1
        assert retrieved[0].content["text"] == text
        assert retrieved[0].content["origin_group"] == "origin_local"
        assert session.get(EvidenceRecord, "evidence_foreign").content["origin_group"] == "origin_foreign"
