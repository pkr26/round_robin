"""Saved report journal selection remains scoped to its run and cutoff."""
from copy import deepcopy
from dataclasses import replace

from fastapi.testclient import TestClient
from round_robin.api.auth import require_owner
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Event, ReportVersion
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.contributions import DiscussionObjection, ObjectionResponse
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def _saved_with_journal(factory, suffix='one'):
    class Provider(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            ref = min(request.allowed_reference_ids)
            output = result.output
            if request.stage == 'proposal' and self.provider == 'openai':
                output = output.model_copy(update={'objections': [DiscussionObjection(alias='new_objection_1', concern='Independent review is needed.', severity='material', reference_ids=[ref])]})
            if request.stage in {'synthesis', 'audit'} and request.open_objections:
                output = output.model_copy(update={'objection_responses': [ObjectionResponse(objection_id=issue['id'], disposition='resolved', response='The passage addresses the question.', reference_ids=[ref]) for issue in request.open_objections if issue['severity'] == 'material']})
            return replace(result, output=output)
    run_id = seed(factory, suffix=suffix)
    drain(factory, {p: Provider(p) for p in ('openai', 'anthropic', 'google')})
    return run_id


def _read_current_report(factory, tmp_path, run_id):
    settings = replace(Settings(), artifact_directory=tmp_path / 'artifacts')
    app = create_app(str(factory.kw['bind'].url), settings=settings)
    app.dependency_overrides[require_owner] = lambda: 'owner_test'
    with TestClient(app) as client:
        return client.get(f'/api/runs/{run_id}/report')


def test_foreign_run_journal_does_not_change_saved_report(factory, tmp_path):
    run_id = _saved_with_journal(factory)
    other_id = _saved_with_journal(factory, suffix='foreign')
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        foreign_journal = session.scalar(select(Event).where(
            Event.run_id == other_id, Event.sequence <= report.event_cutoff,
            Event.event_type == 'discussion.obligation_recorded'))
        assert foreign_journal is not None
        expected_issues = deepcopy(report.content['issues'])
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 200, response.text
    assert response.json()['run_id'] == run_id
    assert response.json()['issues'] == expected_issues


def test_journal_event_at_saved_cutoff_remains_in_scope(factory, tmp_path):
    run_id = _saved_with_journal(factory)
    with factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        last_journal = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == 'discussion.obligation_recorded').order_by(Event.sequence.desc()))
        assert last_journal is not None and last_journal.sequence < report.event_cutoff
        at_cutoff = session.scalar(select(Event).where(Event.run_id == run_id, Event.sequence == report.event_cutoff))
        assert at_cutoff is not None and at_cutoff.event_type == 'report.published'
        # Synthetic history: the normal writer puts report.published here.
        # Move the journal entry to this boundary to exercise inclusive reading.
        session.delete(at_cutoff)
        session.flush()
        last_journal.sequence = report.event_cutoff
        session.flush()
        expected_issues = deepcopy(report.content['issues'])
    response = _read_current_report(factory, tmp_path, run_id)
    assert response.status_code == 200, response.text
    assert response.json()['run_id'] == run_id
    assert response.json()['issues'] == expected_issues
