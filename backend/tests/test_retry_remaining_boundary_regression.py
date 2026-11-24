"""The audit retry uses its remaining active-time boundary."""

from datetime import timedelta

from sqlalchemy import select

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import BudgetOperation, Run, Step, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_audit_retry_is_available_above_25_seconds(factory, monkeypatch):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.stage = "audit"
        hold = session.get(BudgetOperation, f"finish_{run_id}")
        hold.metadata_json = {**hold.metadata_json, "roles_remaining": ["audit"]}
        monkeypatch.setattr("round_robin.orchestration.policies.remaining_active", lambda run: 25)
        assert retryable_steps(session, run) == []
        monkeypatch.setattr("round_robin.orchestration.policies.remaining_active", lambda run: 26)
        assert retryable_steps(session, run) == [step]
