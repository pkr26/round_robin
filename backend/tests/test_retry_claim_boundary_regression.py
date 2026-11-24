"""Manual retry respects the accumulated infrastructure claim limit."""
from datetime import timedelta

from sqlalchemy import select

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import Run, Step, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_retry_stops_at_three_infrastructure_claims(factory):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.infrastructure_claims = 2
        assert retryable_steps(session, run) == [step]
        step.infrastructure_claims = 3
        assert retryable_steps(session, run) == []
