"""A partial report cannot expose forged journal-backed discussion details."""
from copy import deepcopy
from dataclasses import replace

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


def test_redacted_partial_does_not_launder_forged_issue(factory, tmp_path):
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
        original_issue = deepcopy(old.content['issues'][0])
        run = lock_work_run(session, run_id)
        issues = deepcopy(run.configuration['discussion_issues'])
        run.configuration = {**run.configuration, 'discussion_issues': issues + [issues[0]]}
        run.status = 'running'
        hold = session.get(BudgetOperation, f'finish_{run_id}')
        hold.status, hold.reserved_usd = 'held', '0'
        advance_run(session, run)
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id).order_by(ReportVersion.version.desc()))
        assert row.version > old.version and row.content['issues'] == []
        forged = deepcopy(row.content)
        original_issue['concern'] = 'PRIVATE_FORGED_ISSUE_SENTINEL'
        forged['issues'] = [original_issue]
        RuntimeDecisionReport.model_validate(forged)
        row.content = forged
    settings = replace(Settings(), artifact_directory=tmp_path / 'artifacts')
    app = create_app(str(factory.kw['bind'].url), settings=settings)
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app) as client:
        response = client.get(f'/api/runs/{run_id}/report')
    assert response.status_code == 503
    assert 'PRIVATE_FORGED_ISSUE_SENTINEL' not in response.text
