"""Saved JSON export preserves strict numeric and byte-size boundaries."""
import json
import math
from copy import deepcopy
from uuid import uuid4

import pytest
from round_robin.database.models import ExportRecord, ReportVersion
from round_robin.reports.exports import MAX_EXPORT_BYTES, render_saved_report
from round_robin.schemas.runtime import RuntimeDecisionReport
from sqlalchemy import select

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414
from backend.tests.test_assessment_command_integrity_regressions import (
    attestation as attestation,  # noqa: PLC0414
)
from backend.tests.test_assessment_command_integrity_regressions import (
    payload as assessment_payload,
)
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def save_objective(artifacts, note, *, validate=True):
    app, _, _, _, report = artifacts
    with app.state.session_factory.begin() as session:
        row = session.get(ReportVersion, report['id'])
        content = deepcopy(row.content)
        content['research_summary']['objectives'] = [{'note': note}]
        # The current schema intentionally accepts this synthetic historical
        # dictionary; ordinary research emits a smaller structured objective.
        if validate:
            RuntimeDecisionReport.model_validate(content)
        row.content = content


def export(artifacts):
    _, client, _, run_id, report = artifacts
    response = client.post(f'/api/runs/{run_id}/exports', json={'command_id': str(uuid4()), 'report_id': report['id'], 'format': 'json'})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_json_renderer_rejects_nonfinite_numbers_before_producing_bytes(value):
    # Exercise the renderer's strict JSON boundary independently of the
    # saved-report reader, which now rejects the same damaged input earlier.
    with pytest.raises(ValueError):
        render_saved_report({'nested': [value]}, 'json', title='Synthetic')


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_nested_saved_value_is_unavailable_on_every_report_reader(artifacts, value):
    save_objective(artifacts, {'nested': [0, {'corrupt': value, 'sentinel': 'PRIVATE_CORRUPT_VALUE'}]})
    app, client, meeting_id, run_id, report = artifacts
    detail = client.get(f'/api/runs/{run_id}')
    assert detail.status_code == 200 and detail.json()['readiness'] is None
    meeting = client.get(f'/api/meetings/{meeting_id}')
    assert meeting.status_code == 200
    assert next(run for run in meeting.json()['runs'] if run['id'] == run_id)['readiness'] is None
    for path in (f'/api/runs/{run_id}/report', f'/api/runs/{run_id}/reports/1', f'/api/runs/{run_id}/snapshot'):
        response = client.get(path)
        assert response.status_code == 503 and 'PRIVATE_CORRUPT_VALUE' not in response.text
    calculation = client.get(f"/api/runs/{run_id}/calculations/{report['calculations'][0]['id']}?report_id={report['id']}")
    assert calculation.status_code == 409 and 'PRIVATE_CORRUPT_VALUE' not in calculation.text
    for format_name in ('json', 'markdown', 'pdf'):
        response = client.post(f'/api/runs/{run_id}/exports', json={'command_id': str(uuid4()), 'report_id': report['id'], 'format': format_name})
        assert response.status_code == 409 and response.json()['error']['code'] == 'report_invalid'
        assert 'PRIVATE_CORRUPT_VALUE' not in response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(ExportRecord)) is None
        saved = session.get(ReportVersion, report['id']).content['research_summary']['objectives'][0]['note']['nested'][1]['corrupt']
        assert math.isnan(saved) if math.isnan(value) else saved == value


def test_finite_nested_json_values_and_numeric_types_remain_exact(artifacts):
    note = {'nested': [0, -37, 1.25, -3.5, 1e100, True, False, None, 'NaN']}
    save_objective(artifacts, note)
    result = export(artifacts)
    assert result['status'] == 'ready', result
    _, client, *_ = artifacts
    response = client.get(result['download_url'])
    assert response.status_code == 200
    saved = json.loads(response.content)['research_summary']['objectives'][0]['note']['nested']
    assert saved == note['nested']
    assert [type(item) for item in saved] == [type(item) for item in note['nested']]


@pytest.mark.parametrize('corrupt', [False, True])
def test_nonfinite_old_report_blocks_assessment_without_committing_it(attestation, corrupt):
    db_factory, run_id, client = attestation
    payload = assessment_payload(client, run_id)
    with db_factory.begin() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        report_version = report.version
        if corrupt:
            content = deepcopy(report.content)
            content['research_summary']['objectives'] = [{'nested': [float('nan')]}]
            report.content = content
    response = client.post(f'/api/runs/{run_id}/assessments', json=payload)
    assert response.status_code == (503 if corrupt else 201), response.text
    with db_factory() as session:
        from round_robin.database.models import Command, Run
        run = session.get(Run, run_id)
        if corrupt:
            assert not run.configuration.get('evidence_assessments')
            assert run.report_version == report_version
            assert session.get(Command, ('owner_test', payload['command_id'])) is None
        else:
            assert run.report_version == report_version + 1
            assert session.get(Command, ('owner_test', payload['command_id'])) is not None


@pytest.mark.parametrize('depth', [40, 600])
def test_nested_saved_structure_remains_bounded_and_sanitized(artifacts, depth):
    nested = 'bounded sentinel'
    for _ in range(depth):
        nested = [nested]
    save_objective(artifacts, nested, validate=False)
    _, client, _, run_id, _ = artifacts
    response = client.get(f'/api/runs/{run_id}/report')
    if depth == 40:
        assert response.status_code == 200, response.text
        assert response.json()['research_summary']['objectives'][0]['note'] == nested
    else:
        assert response.status_code == 503
        assert 'bounded sentinel' not in response.text


def test_unicode_json_is_preserved_within_real_artifact_size_limit(artifacts):
    # Escaping each character would exceed the true ten-MiB limit, while the
    # valid UTF-8 representation is much smaller. This targets a synthetic
    # large but current-schema-valid saved objective, not a normal search hit.
    note = 'é' * (MAX_EXPORT_BYTES // 6 + 100)
    save_objective(artifacts, note)
    result = export(artifacts)
    assert result['status'] == 'ready', result
    _, client, *_ = artifacts
    response = client.get(result['download_url'])
    assert response.status_code == 200
    assert len(response.content) < MAX_EXPORT_BYTES
    assert json.loads(response.content)['research_summary']['objectives'][0]['note'] == note


@pytest.mark.parametrize('excess,expected', [(0, 'ready'), (1, 'failed')])
def test_real_ten_mib_boundary_accepts_equality_and_rejects_one_extra_byte(artifacts, excess, expected):
    _, client, _, _, report = artifacts
    empty = deepcopy(report)
    empty['research_summary']['objectives'] = [{'note': ''}]
    validated = RuntimeDecisionReport.model_validate(empty).model_dump(mode='json')
    baseline = len(render_saved_report(validated, 'json', title='Synthetic')[0])
    note = 'x' * (MAX_EXPORT_BYTES - baseline + excess)
    save_objective(artifacts, note)
    result = export(artifacts)
    assert result['status'] == expected, result
    if expected == 'ready':
        response = client.get(result['download_url'])
        assert response.status_code == 200 and len(response.content) == MAX_EXPORT_BYTES
        assert json.loads(response.content)['research_summary']['objectives'][0]['note'] == note
    else:
        assert result['error'] == 'export_render_failed' and result['download_url'] is None
