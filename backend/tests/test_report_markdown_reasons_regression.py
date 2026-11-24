from backend.tests.test_runtime_report import runtime_body
from backend.tests.test_orchestration import factory
from round_robin.reports.exports import render_saved_report
from round_robin.schemas.runtime import RuntimeDecisionReport


def test_markdown_export_preserves_saved_main_reasons(runtime_body):
    runtime_body["main_reasons"] = ["The first proposal meets the deadline.", "Its operating cost is known."]
    report = RuntimeDecisionReport.model_validate(runtime_body).model_dump(mode="json")

    artifact, media_type = render_saved_report(report, "markdown", title="Saved decision", sources=[])
    text = artifact.decode("utf-8")

    assert media_type == "text/markdown; charset=utf-8"
    assert "The first proposal meets the deadline" in text
    assert "Its operating cost is known" in text
