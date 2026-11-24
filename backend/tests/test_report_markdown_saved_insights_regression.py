from backend.tests.test_runtime_report import runtime_body
from backend.tests.test_orchestration import factory
from round_robin.reports.exports import render_saved_report
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_markdown_preserves_saved_readiness_scope_and_decision_insight(runtime_body):
    runtime_body['readiness_dimensions'] = {
        'complete_comparison': True,
        'checked_arithmetic': True,
        'scoped_cost_support': True,
        'requirements_assessed': True,
        'distinct_review': True,
        'material_objections_resolved': True,
        'basis_scope': 'Based on the current site estimate only.',
    }
    runtime_body['decision_insights']['limitations'] = ['Ordering excludes unquoted installation work.']
    report = RuntimeDecisionReport.model_validate(runtime_body).model_dump(mode='json')

    artifact, media_type = render_saved_report(report, 'markdown', title='Saved decision', sources=[])
    text = artifact.decode('utf-8')
    assert media_type == 'text/markdown; charset=utf-8'
    assert 'Based on the current site estimate only' in text
    assert 'Ordering excludes unquoted installation work' in text
