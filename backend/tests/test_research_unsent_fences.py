import pytest
from round_robin.database.models import BudgetOperation, Run
from round_robin.research import service
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend.tests.test_research_service import PageFixture, SearchFixture
from backend.tests.test_research_service import research_run as research_run  # noqa: PLC0414


@pytest.mark.parametrize("race", ["cancel_requested", "lease_lost", "fence_changed"])
def test_known_unsent_search_after_concurrent_fence(research_run, monkeypatch, race):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
    orig = service.append_event

    def append(session, run, event_type, payload):
        result = orig(session, run, event_type, payload)
        if event_type == "research.query_authorized":
            session.info["research_race_after_commit"] = True
        return result

    def after_commit(session):
        if not session.info.pop("research_race_after_commit", False):
            return
        with research_run.begin() as concurrent:
            run = concurrent.get(Run, "run_local")
            if race == "cancel_requested":
                run.status = "cancel_requested"
                run.stop_reason = "user_cancellation"
                run.execution_fence += 1
            elif race == "lease_lost":
                run.configuration = {
                    **run.configuration,
                    "research_lease_id": "superseded",
                }
            else:
                run.execution_fence += 1

    monkeypatch.setattr(service, "append_event", append)
    event.listen(Session, "after_commit", after_commit)
    search = SearchFixture()
    try:
        assert not service.execute_research(
            "run_local",
            session_factory=research_run,
            search=search,
            transport=PageFixture(),
            resolver=lambda _: ["93.184.216.34"],
        )
    finally:
        event.remove(Session, "after_commit", after_commit)
    assert search.queries == []
    with research_run() as session:
        op = session.scalar(
            select(BudgetOperation).where(BudgetOperation.kind == "public_search")
        )
        run = session.get(Run, "run_local")
        assert op.status == "settled" and op.settled_usd == "0"
        if race == "cancel_requested":
            assert run.status == "cancelled"


@pytest.mark.parametrize("settlement", ["unknown", "settled"])
def test_concurrent_settlement_is_not_replaced_by_unsent_observation(
    research_run, monkeypatch, settlement
):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
    original = service.append_event

    def append(session, run, event_type, payload):
        value = original(session, run, event_type, payload)
        if event_type == "research.query_authorized":
            session.info["settle_search_after_commit"] = payload["operation_id"]
        return value

    def after_commit(session):
        operation_id = session.info.pop("settle_search_after_commit", None)
        if not operation_id:
            return
        with research_run.begin() as concurrent:
            run = concurrent.get(Run, "run_local")
            run.execution_fence += 1
            operation = concurrent.get(BudgetOperation, operation_id)
            operation.status = settlement
            operation.settled_usd = "0.01" if settlement == "settled" else None

    monkeypatch.setattr(service, "append_event", append)
    event.listen(Session, "after_commit", after_commit)
    try:
        search = SearchFixture()
        assert not service.execute_research(
            "run_local",
            session_factory=research_run,
            search=search,
            transport=PageFixture(),
            resolver=lambda _: ["93.184.216.34"],
        )
    finally:
        event.remove(Session, "after_commit", after_commit)
    assert search.queries == []
    with research_run() as session:
        operation = session.scalar(
            select(BudgetOperation).where(BudgetOperation.kind == "public_search")
        )
        assert operation.status == settlement
        assert operation.settled_usd == ("0.01" if settlement == "settled" else None)
