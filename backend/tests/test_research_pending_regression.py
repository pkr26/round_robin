"""Queued research publishes its pending state and dispatch together."""
from sqlalchemy import select

from round_robin.database.models import Outbox, Run
from round_robin.research.service import schedule_research
from test_research_service import research_run


def test_scheduling_persists_pending_summary_and_dispatch(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert schedule_research(session, run) is True

    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_state"] == "pending"
        assert run.configuration["research_summary"] == {
            "status": "pending", "queries": 0, "pages": 0,
        }
        deliveries = list(session.scalars(select(Outbox)))
        assert [(row.run_id, row.kind, row.status) for row in deliveries] == [
            ("run_local", "research", "pending")
        ]
