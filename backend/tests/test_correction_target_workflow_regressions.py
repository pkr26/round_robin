import pytest
from round_robin.database.models import BudgetOperation, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from sqlalchemy import select

from backend.tests.test_discussion_integrity import prepare_resolved_issue

pytest_plugins = ("backend.tests.test_orchestration",)


@pytest.mark.parametrize("severity,status,correction", [
    ("material", "open", True),
    ("critical", "answered_pending_review", True),
    ("minor", "open", False),
    ("material", "resolved", False),
])
def test_controller_schedules_only_actionable_obligations(factory, monkeypatch, severity, status, correction):
    run_id = prepare_resolved_issue(factory, monkeypatch, severity=severity, target_status=status)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        if correction:
            assert run.status == "running" and run.stage == "synthesis"
            assert run.configuration["stage_iteration"] == 1
            assert session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "synthesis", Step.stage_iteration == 1)) is not None
            hold = session.get(BudgetOperation, f"finish_{run_id}")
            assert hold.status == "held" and set(hold.metadata_json["roles_remaining"]) == {"synthesis", "audit"}
        else:
            assert run.status in {"completed", "completed_with_limitations"}
            assert not run.configuration.get("stage_iteration")
