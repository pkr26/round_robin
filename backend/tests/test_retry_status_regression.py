"""Retry requires both a failed status and a retryable failure."""

from datetime import timedelta

from sqlalchemy import select

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import Run, Step, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_retry_requires_failed_status_and_retryable_error(factory):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        assert retryable_steps(session, run) == [step]
        step.error_code = "authentication"
        assert retryable_steps(session, run) == []
        step.error_code = "worker_lost_after_authorization"
        step.status = "pending"
        assert retryable_steps(session, run) == []
