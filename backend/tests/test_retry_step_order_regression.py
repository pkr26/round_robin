"""A preceding non-retryable step must not hide a later retry."""

from datetime import timedelta

from sqlalchemy import select

from backend.tests.test_audit_runtime_fixes import retained_terminal_run
from backend.tests.test_orchestration import factory
from round_robin.database.models import Run, Step, utcnow
from round_robin.orchestration.policies import retryable_steps


def test_retry_search_continues_after_non_finish_step(factory):
    run_id = retained_terminal_run(factory, expires_at=utcnow() + timedelta(hours=1))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        retry = session.scalar(select(Step).where(Step.run_id == run_id))
        earlier = Step(id="earlier_discussion_step", run_id=run_id, stage="proposal", round=0,
                       stage_iteration=0, role_slot_id="participant", status="succeeded", calls=1,
                       infrastructure_claims=1, generation=run.recovery_generation)
        session.add(earlier)
        session.flush()
        assert retryable_steps(session, run, steps=[earlier, retry]) == [retry]
