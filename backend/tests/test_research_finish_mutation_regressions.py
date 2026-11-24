"""Research must settle visibly without spending when finishing time is reserved."""

import pytest
from round_robin.database.models import BudgetOperation, Event, Outbox, Run
from round_robin.research import service
from sqlalchemy import func, select

pytest_plugins = ("test_research_service",)


@pytest.mark.parametrize("via", ["worker", "reconciler"])
def test_unstarted_research_preserves_finishing_budget_and_emits_unverified_gap(research_run, via):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        run.active_seconds, run.active_started_at = 400, None
        run.configuration = {**run.configuration, "research_lease_id": "stale-synthetic-lease"}

    class NoSearch:
        def search(self, *args, **kwargs):
            pytest.fail("Research must not send a search after finishing time is reserved")

    if via == "worker":
        assert not service.execute_research("run_local", session_factory=research_run, search=NoSearch())
    else:
        with research_run.begin() as session:
            assert service.reconcile_public_research(session, session.get(Run, "run_local"))

    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_state"] == "done"
        assert run.configuration["research_lease_id"] is None
        assert run.configuration["research_summary"] == {
            "status": "unavailable", "reason": "finish_time_reserve", "queries": 0, "pages": 0,
        }
        events = list(session.scalars(select(Event).where(Event.run_id == run.id, Event.event_type == "research.unavailable")))
        assert len(events) == 1
        assert events[0].payload == {"reason": "finish_time_reserve", "verified": False}
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.run_id == run.id, Outbox.kind == "advance")) == 1
        assert session.scalar(select(func.count()).select_from(BudgetOperation)) == 0
