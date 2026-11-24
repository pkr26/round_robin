"""A redacted partial cannot publish unvalidated derived content."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import BudgetOperation, ReportVersion, Run, utcnow
from round_robin.database.services import canonical_hash
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.simulated import SimulatedProvider
from round_robin.research.reuse import StoredClaim
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


@pytest.mark.parametrize('field', ['calculations', 'claim_ids', 'essential_dependencies'])
def test_redacted_partial_cannot_launder_derived_content(factory, tmp_path, field):
    run_id = _redacted_partial(factory)
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id).order_by(ReportVersion.version.desc()))
        run = session.get(Run, run_id)
        forged = deepcopy(row.content)
        if field == 'calculations':
            forged['calculations'] = [{'id': 'calc_private_forged', 'label': 'private_forged_sentinel',
                                       'operation': 'add', 'inputs': [], 'formula': '1', 'output': '1',
                                       'unit': 'USD', 'currency': 'USD', 'rounding': 'exact'}]
        elif field == 'claim_ids':
            claim = StoredClaim.model_validate({'id': 'claim_private_forged', 'version': 1,
                                                'proposition': 'private_forged_sentinel',
                                                'kind': 'assumption', 'evidence_status': 'not_checked',
                                                'time_sensitive': False, 'verified_at': utcnow().isoformat()}).model_dump(mode='json', exclude_unset=True)
            session.add(ClaimVersion(id=claim['id'], version=1, run_id=run_id,
                                     meeting_id=run.meeting_id, owner_id=run.owner_id, step_id=None,
                                     content=claim, sha256=canonical_hash(claim),
                                     created_at=row.created_at - timedelta(seconds=1)))
            session.flush()
            forged['claim_ids'] = [claim['id']]
        else:
            forged['essential_dependencies'] = [{'id': 'dependency_private_forged',
                                                  'description': 'private_forged_sentinel',
                                                  'status': 'unverified', 'accepted_scoped_support': False,
                                                  'reference_ids': [], 'origin': 'user_supplied'}]
        RuntimeDecisionReport.model_validate(forged)
        row.content = forged
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 503
    assert 'private_forged' not in response.text
