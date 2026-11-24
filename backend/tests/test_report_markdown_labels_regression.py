from backend.tests.test_runtime_report import runtime_body
from backend.tests.test_orchestration import factory
from round_robin.reports.exports import render_saved_report
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_markdown_preserves_saved_comparison_option_labels(runtime_body):
    assert len(runtime_body['comparison']) >= 2
    labels = [f'Array choice {index}' for index in range(len(runtime_body['comparison']))]
    for option, label in zip(runtime_body['comparison'], labels, strict=True):
        option['label'] = label
        assert option['option_id'] != label
    report = RuntimeDecisionReport.model_validate(runtime_body).model_dump(mode='json')

    artifact, media_type = render_saved_report(report, 'markdown', title='Saved decision', sources=[])
    text = artifact.decode('utf-8')
    assert media_type == 'text/markdown; charset=utf-8'
    for label in labels:
        assert f'### {label}\n' in text
