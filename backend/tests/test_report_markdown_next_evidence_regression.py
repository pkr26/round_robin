"""Saved follow-up questions and their basis in published reports."""
from io import BytesIO

from pydantic import ValidationError
from pypdf import PdfReader
import pytest

from backend.tests.test_runtime_report import runtime_body
from backend.tests.test_orchestration import factory
from round_robin.reports.exports import render_saved_report
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_markdown_keeps_saved_next_evidence_questions_as_headings(runtime_body):
    runtime_body['next_evidence'] = [
        {'id': 'next_quote_check', 'question': 'Confirm the complete quote',
         'affected_conclusion': 'Cost ordering may change', 'verification_method': 'Request written quote',
         'request_text': 'Please list installation charges'},
        {'id': 'next_site_check', 'question': 'Inspect the installation site',
         'affected_conclusion': 'Feasibility may change', 'verification_method': 'Arrange site visit',
         'request_text': 'Please inspect the available clearance'},
    ]
    runtime_body['next_evidence_count'] = 2
    report = RuntimeDecisionReport.model_validate(runtime_body).model_dump(mode='json')
    artifact, media_type = render_saved_report(report, 'markdown', title='Saved decision', sources=[])
    text = artifact.decode('utf-8')
    assert media_type == 'text/markdown; charset=utf-8'
    for item in report['next_evidence']:
        assert f"### {item['question']}\n" in text
        assert item['request_text'] in text


def test_report_exports_keep_ranked_question_count_and_scoped_citations(runtime_body):
    calculation = runtime_body['calculations'][0]
    passage_id = calculation['inputs'][0]['source_ref']
    runtime_body['tradeoff_basis_refs'] = [calculation['id']]
    runtime_body['next_evidence'] = [
        {'id': f'next_check_{index}', 'question': f'Confirm charge {index}',
         'affected_conclusion': 'Could exceed the budget', 'verification_method': 'Request a signed quote',
         'request_text': f'Please confirm charge {index} in writing', 'basis_refs': [calculation['id'], passage_id]}
        for index in range(1, 21)
    ]
    runtime_body['next_evidence_count'] = 23
    report = RuntimeDecisionReport.model_validate(runtime_body).model_dump(mode='json')
    for format in ('markdown', 'pdf'):
        content, _ = render_saved_report(report, format, title='Installation costs')
        text = (content.decode().replace('\\', '') if format == 'markdown'
                else ' '.join(page.extract_text() for page in PdfReader(BytesIO(content)).pages))
        text = ''.join(text.split())
        assert '23outstandingquestions.Showingthefirst20.' in text
        assert text.index('Confirmcharge1') < text.index('Confirmcharge20')
        assert f"Basis:{calculation['id']},{passage_id}" in text
        assert f"Calculation{calculation['id']}" in text

    legacy = dict(report)
    legacy.pop('next_evidence_count')
    legacy.pop('tradeoff_basis_refs')
    legacy['next_evidence'] = [{k: v for k, v in item.items() if k != 'basis_refs'} for item in report['next_evidence']]
    reopened = RuntimeDecisionReport.model_validate(legacy)
    assert reopened.next_evidence_count is None and not reopened.tradeoff_basis_refs
    assert all(not item.basis_refs for item in reopened.next_evidence)
    assert 'outstanding questions' not in render_saved_report(legacy, 'markdown', title='Historical report')[0].decode()
    with pytest.raises(ValidationError, match='count must cover'):
        RuntimeDecisionReport.model_validate({**report, 'next_evidence_count': 0})
    with pytest.raises(ValidationError, match='citations must resolve uniquely'):
        RuntimeDecisionReport.model_validate({**report, 'tradeoff_basis_refs': ['outside_the_report']})
    with pytest.raises(ValidationError, match='citations must resolve uniquely'):
        RuntimeDecisionReport.model_validate({**report, 'next_evidence': [{**report['next_evidence'][0], 'basis_refs': [passage_id, passage_id]}]})
