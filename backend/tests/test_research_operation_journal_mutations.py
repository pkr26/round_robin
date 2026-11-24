"""Search authorizations remain recoverable across a later interrupted request."""
import pytest
from round_robin.database.models import BudgetOperation, Event, Run
from round_robin.research import service
from sqlalchemy import select

pytest_plugins = ("test_research_service",)


def test_search_operations_survive_third_request_interruption_and_recovery(research_run):
    with research_run.begin() as session:
        assert service.schedule_research(session, session.get(Run, "run_local"))

    class InterruptedSearch:
        def __init__(self):
            self.queries = []

        def search(self, query, **kwargs):
            self.queries.append(query)
            if len(self.queries) == 3:
                raise RuntimeError("synthetic interruption after authorization")
            return []

    search = InterruptedSearch()
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        service.execute_research("run_local", session_factory=research_run, search=search)
    assert len(search.queries) == 3
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_state"] == "running"
        ids = run.configuration["research_operation_ids"]
        assert len(ids) == 3 and len(set(ids)) == 3
        operations = {row.id: row for row in session.scalars(select(BudgetOperation).where(BudgetOperation.run_id == run.id, BudgetOperation.kind == "public_search"))}
        assert set(operations) == set(ids)
        assert all(operations[identity].kind == "public_search" for identity in ids)
        assert [operations[identity].status for identity in ids] == ["unknown", "unknown", "reserved"]
        assert all(operations[identity].metadata_json.get("model_call") is False for identity in ids)
        events = list(session.scalars(select(Event).where(Event.run_id == run.id, Event.event_type == "research.query_authorized").order_by(Event.sequence)))
        plan = run.configuration["research_plan"]
        assert [event.payload for event in events] == [
            {"operation_id": identity, "template_id": item["template_id"], "term_ids": item["term_ids"]}
            for identity, item in zip(ids, plan, strict=True)
        ]
        run.configuration = {**run.configuration, "research_lease_until": "2000-01-01T00:00:00+00:00"}
        assert service.reconcile_public_research(session, run)
    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_state"] == "done"
        assert run.configuration["research_operation_ids"] == ids
        assert all(session.get(BudgetOperation, identity).status == "unknown" for identity in ids)
    assert not service.execute_research("run_local", session_factory=research_run, search=search)
    assert len(search.queries) == 3
