"""A redacted partial cannot publish unvalidated decision insights."""
from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import BudgetOperation, ReportVersion
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection
from round_robin.schemas.runtime import RuntimeDecisionReport
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def _redacted_partial(factory):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == 'proposal' and self.provider == 'openai':
                output = result.output.model_copy(update={'objections': [DiscussionObjection(alias='new_objection_1', concern='Independent review needed.', severity='material', reference_ids=[])]})
                result = replace(result, output=output)
            return result

    run_id = seed(factory)
    drain(factory, {p: Provider(p) for p in ('openai', 'anthropic', 'google')})
    with factory.begin() as session:
        old = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id).order_by(ReportVersion.version.desc()))
        run = lock_work_run(session, run_id)
        issues = deepcopy(run.configuration['discussion_issues'])
        run.configuration = {**run.configuration, 'discussion_issues': issues + [issues[0]]}
        run.status = 'running'
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        hold.status, hold.reserved_usd = 'held', '0'
        advance_run(session, run)
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id).order_by(ReportVersion.version.desc()))
        assert row.version > old.version and row.content['issues'] == []
    return run_id


def _read_current_report(factory, tmp_path, run_id):
    settings = replace(Settings(), artifact_directory=tmp_path / 'artifacts')
    app = create_app(str(factory.kw['bind'].url), settings=settings)
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app) as client:
        return client.get(f'/api/runs/{run_id}/report')


@pytest.mark.parametrize('field,value', [
    ('cost_frontier_option_ids', ['private_forged_sentinel']),
    ('dominated_option_ids', ['private_forged_sentinel']),
    ('unresolved_option_ids', ['private_forged_sentinel']),
    ('preference_order', ['private_forged_sentinel']),
    ('sensitivity', [{'option_id': 'option_a', 'requirement_id': 'requirement_a',
                      'threshold': '1', 'total': '2', 'margin': '1',
                      'unit': 'USD', 'scope': 'private_forged_sentinel'}]),
])
def test_redacted_partial_cannot_launder_insights(factory, tmp_path, field, value):
    run_id = _redacted_partial(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id).order_by(ReportVersion.version.desc()))
        forged = deepcopy(row.content)
        forged['decision_insights'] = {**forged.get('decision_insights', {}), field: value}
        RuntimeDecisionReport.model_validate(forged)
        row.content = forged
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 503
    assert 'private_forged_sentinel' not in response.text


def test_legacy_partial_without_insights_remains_readable(factory, tmp_path):
    run_id = _redacted_partial(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id).order_by(ReportVersion.version.desc()))
        legacy = deepcopy(row.content)
        legacy.pop('decision_insights', None)
        RuntimeDecisionReport.model_validate(legacy)
        row.content = legacy
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 200, response.text
    assert response.json()['decision_insights']['cost_frontier_option_ids'] == []
    assert response.json()['issues'] == []
