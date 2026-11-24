"""Scheduling preserves existing work and records no-request state."""
import pytest
from sqlalchemy import func, select
from round_robin.database.models import Outbox, Run
from round_robin.research.service import schedule_research
from test_research_service import research_run

@pytest.mark.parametrize('state', ['pending', 'running'])
def test_existing_research_remains_untouched(research_run, state):
    with research_run.begin() as session:
        run = session.get(Run, 'run_local')
        run.configuration = {**run.configuration, 'research_state': state, 'research_plan': [{'sentinel': 'original'}], 'research_lease_id': 'existing'}
        before = dict(run.configuration)
        assert schedule_research(session, run) is True
        assert run.configuration == before
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0

def test_no_public_terms_records_not_requested_without_dispatch(research_run):
    with research_run.begin() as session:
        run = session.get(Run, 'run_local')
        run.input_snapshot = {**run.input_snapshot, 'public_research_terms': []}
        assert schedule_research(session, run) is False
        assert run.configuration['research_state'] == 'done'
        assert run.configuration['research_summary'] == {'status': 'not_requested', 'queries': 0, 'pages': 0}
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0

def test_live_run_without_research_policy_is_unavailable(research_run):
    with research_run.begin() as session:
        run = session.get(Run, 'run_local')
        run.configuration = {key: value for key, value in run.configuration.items() if key != 'research'}
        assert schedule_research(session, run) is False
        assert run.configuration['research_state'] == 'done'
        assert run.configuration['research_summary']['reason'] == 'simulation_or_unconfigured_research'
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0
