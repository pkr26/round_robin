"""Check saved follow-up questions retain their headings in Markdown."""
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
    report = RuntimeDecisionReport.model_validate(runtime_body).model_dump(mode='json')
    artifact, media_type = render_saved_report(report, 'markdown', title='Saved decision', sources=[])
    text = artifact.decode('utf-8')
    assert media_type == 'text/markdown; charset=utf-8'
    for item in report['next_evidence']:
        assert f"### {item['question']}\n" in text
        assert item['request_text'] in text
