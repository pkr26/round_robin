"""Candidate: scoped saved evidence and change history survive Markdown export."""
from backend.tests.test_runtime_report import runtime_body
from backend.tests.test_orchestration import factory
from round_robin.reports.exports import render_saved_report
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_markdown_preserves_saved_scoped_assessment_and_change_summary(runtime_body):
    evidence = runtime_body['evidence_ids'][0]
    runtime_body['change_summary'] = ['Installation estimate was added since the parent report.']
    runtime_body['evidence_assessments'] = [{
        'id': 'assessment_export_test', 'target_type': 'option_costs',
        'option_id': runtime_body['comparison'][0]['option_id'], 'requirement_id': None,
        'target_hash': 'a' * 64, 'reference_ids': [evidence],
        'evidence_hashes': {evidence: 'b' * 64}, 'result': 'supported',
        'attestation': 'The owner checked this exact cost evidence.', 'actor_id': 'owner_test',
        'run_id': runtime_body['run_id'], 'recorded_at': '2026-01-01T00:00:00Z',
    }]
    report = RuntimeDecisionReport.model_validate(runtime_body).model_dump(mode='json')

    artifact, media_type = render_saved_report(report, 'markdown', title='Saved decision', sources=[])
    text = artifact.decode('utf-8')
    assert media_type == 'text/markdown; charset=utf-8'
    assert 'Installation estimate was added since the parent report' in text
    assert 'The owner checked this exact cost evidence' in text
