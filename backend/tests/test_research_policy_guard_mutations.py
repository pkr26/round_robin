"""Research requires every live policy guard before queuing work."""
import pytest
from sqlalchemy import func, select

from round_robin.database.models import Outbox, Run
from round_robin.research.service import schedule_research
from test_research_service import research_run


@pytest.mark.parametrize('override', [
    {'enabled': False},
    {'pricing_version': None},
    {'per_request_usd': None},
], ids=['disabled', 'missing-pricing-version', 'missing-price'])
def test_incomplete_live_policy_never_enqueues_research(research_run, override):
    with research_run.begin() as session:
        run = session.get(Run, 'run_local')
        run.configuration = {
            **run.configuration,
            'research': {**run.configuration['research'], **override},
        }
        assert schedule_research(session, run) is False
        assert run.configuration['research_state'] == 'done'
        assert run.configuration['research_summary'] == {
            'status': 'unavailable', 'reason': 'simulation_or_unconfigured_research',
            'queries': 0, 'pages': 0,
        }
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0
