"""A full evidence packet prevents spending and records an explicit research gap."""
import pytest
from round_robin.database.models import (
    BudgetOperation,
    Event,
    EvidenceRecord,
    Outbox,
    Run,
)
from round_robin.research import service
from sqlalchemy import func, select

pytest_plugins = ("test_research_service",)


def test_full_packet_settles_without_search_and_records_unverified_limitation(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        run.configuration = {**run.configuration, "research_lease_id": "stale-synthetic-lease"}
        for index in range(24):
            session.add(EvidenceRecord(id=f"evidence_{index}", run_id=run.id, meeting_id=run.meeting_id,
                                       owner_id=run.owner_id, sha256="a" * 64, content={}))

    class NoSearch:
        def search(self, *args, **kwargs):
            pytest.fail("A full evidence packet cannot dispatch public research")

    class NoTransport:
        def get(self, *args, **kwargs):
            pytest.fail("A full evidence packet cannot fetch a source")

    def no_resolver(*args, **kwargs):
        pytest.fail("A full evidence packet cannot resolve a source")

    assert service.execute_research("run_local", session_factory=research_run,
                                    search=NoSearch(), transport=NoTransport(), resolver=no_resolver)
    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_state"] == "done"
        assert run.configuration["research_lease_id"] is None
        assert run.configuration["research_summary"] == {
            "status": "limited", "reason": "passage_packet_limit", "queries": 0,
            "pages": 0, "limitations": ["passage_packet_limit"],
        }
        events = list(session.scalars(select(Event).where(Event.run_id == run.id, Event.event_type == "research.unavailable")))
        assert len(events) == 1
        assert events[0].payload == {"reason": "passage_packet_limit", "verified": False}
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.run_id == run.id, Outbox.kind == "advance")) == 1
        assert session.scalar(select(func.count()).select_from(BudgetOperation)) == 0
        assert session.scalar(select(func.count()).select_from(EvidenceRecord).where(EvidenceRecord.run_id == run.id)) == 24
