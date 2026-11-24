from copy import deepcopy
from uuid import uuid4

from round_robin.database.models import Event, ReportVersion
from sqlalchemy import func, select

pytest_plugins = ('backend.tests.test_artifacts_api',)


def test_tampered_journal_saved_response_is_sanitized_and_export_rejected(artifacts):
    app, client, _, run_id, report = artifacts
    with app.state.session_factory.begin() as session:
        row = session.get(ReportVersion, report['id'])
        sequence = max(row.event_cutoff, session.scalar(select(func.max(Event.sequence)).where(Event.run_id == run_id)) or 0) + 1
        row.event_cutoff = sequence
        row.content = {**deepcopy(row.content), 'event_cutoff': sequence}
        session.add(Event(id='private_journal', run_id=run_id, sequence=sequence,
            event_type='discussion.obligation_recorded', payload={'objection_id':'PRIVATE_SENTINEL','event':{'id':'PRIVATE_SENTINEL'}}))
    response = client.get(f'/api/runs/{run_id}/snapshot')
    assert response.status_code == 503 and response.json()['error']['code'] == 'saved_result_unavailable'
    assert 'PRIVATE_SENTINEL' not in response.text
    response = client.post(f'/api/runs/{run_id}/exports', json={'command_id':str(uuid4()),'report_id':report['id'],'format':'json'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'report_invalid'
    assert 'PRIVATE_SENTINEL' not in response.text
