"""Sparse defaulted saved issue fields remain readable when provenance agrees."""
from copy import deepcopy
from dataclasses import replace

import pytest
from round_robin.database.models import ReportVersion
from round_robin.providers.simulated import SimulatedProvider
from round_robin.reports.read_adapters import read_runtime_report
from round_robin.schemas.contributions import DiscussionObjection
from round_robin.schemas.runtime import RuntimeDecisionReport
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_report_journal_selection_mutation_regressions import _read_current_report


@pytest.mark.parametrize('field', ['reference_ids', 'target_reference_id', 'option_id', 'requirement_id'])
def test_sparse_saved_issue_defaults_match_opening(factory, tmp_path, field):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == 'proposal' and self.provider == 'openai':
                objection = DiscussionObjection(alias='new_objection_1', concern='Independent review is needed.', severity='minor', reference_ids=[])
                result = replace(result, output=result.output.model_copy(update={'objections': [objection]}))
            return result

    run_id = seed(factory)
    drain(factory, {p: Provider(p) for p in ('openai', 'anthropic', 'google')})
    with factory.begin() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        sparse = deepcopy(row.content)
        issue = sparse['issues'][0]
        assert issue[field] == ([] if field == 'reference_ids' else None)
        del issue[field]
        RuntimeDecisionReport.model_validate(sparse)
        row.content = sparse
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert read_runtime_report(session, row)['issues'][0][field] == ([] if field == 'reference_ids' else None)
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 200, response.text
    assert response.json()['issues'][0][field] == ([] if field == 'reference_ids' else None)
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert field not in row.content['issues'][0]


def test_dissent_opening_with_no_reference_field_is_readable(factory, tmp_path):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            if request.stage == 'synthesis':
                result = replace(result, output=result.output.model_copy(update={'dissent': ['A separate minor concern.']}))
            return result

    run_id = seed(factory)
    drain(factory, {p: Provider(p) for p in ('openai', 'anthropic', 'google')})
    with factory() as session:
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert any(issue['origin'] == 'dissent' and issue['reference_ids'] == [] for issue in row.content['issues'])
        result = read_runtime_report(session, row)
        assert any(issue['origin'] == 'dissent' for issue in result['issues'])
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 200, response.text
    assert any(issue['origin'] == 'dissent' for issue in response.json()['issues'])
