"""Unavailable scheduling must emit an explicit unverified event."""
from sqlalchemy import select

from round_robin.database.models import Event, Outbox, Run
from round_robin.research.service import schedule_research
from test_research_service import research_run


def test_simulated_research_emits_unverified_unavailable_event(research_run):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        run.configuration = {**run.configuration, "mode": "simulated"}
        assert schedule_research(session, run) is False
    with research_run() as session:
        events = list(session.scalars(select(Event).where(Event.run_id == "run_local")))
        assert [(event.event_type, event.payload) for event in events] == [
            ("research.unavailable", {"reason": "simulation_or_unconfigured_research", "verified": False})
        ]
        assert list(session.scalars(select(Outbox).where(Outbox.run_id == "run_local"))) == []


def test_unrepresentable_scope_is_settled_once_with_unverified_reason(research_run):
    from round_robin.schemas.briefs import DraftBrief
    from round_robin.schemas.provider_configuration import ResearchConfiguration

    term = " ".join(["x"] * 60)
    region = " ".join(["r"] * 60)
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        brief = {**run.input_snapshot, "public_research_terms": [{"id": "term_one", "text": term}]}
        policy = {**run.configuration["research"], "approved_public_region": region}
        DraftBrief.model_validate(brief)
        ResearchConfiguration.model_validate(policy)
        run.input_snapshot = brief
        run.configuration = {**run.configuration, "research": policy}
        assert schedule_research(session, run) is False
        assert run.configuration["research_state"] == "done"
        assert run.configuration["research_summary"] == {
            "status": "unavailable", "reason": "approved_query_exceeds_bounds", "queries": 0, "pages": 0,
        }
        assert schedule_research(session, run) is False
    with research_run() as session:
        events = list(session.scalars(select(Event).where(Event.run_id == "run_local")))
        assert [(event.event_type, event.payload) for event in events] == [
            ("research.unavailable", {"reason": "approved_query_exceeds_bounds", "verified": False})
        ]
        assert list(session.scalars(select(Outbox).where(Outbox.run_id == "run_local"))) == []
