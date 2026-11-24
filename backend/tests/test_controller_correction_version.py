from round_robin.database.models import Run, Step
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_exhaustive_runtime_correction import CorrectingProvider
from backend.tests.test_orchestration import elapse_retry_wait, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_scheduling_audit_correction_advances_run_version_once(factory):
    run_id = seed(factory)
    registry = {name: CorrectingProvider(name) for name in ("openai", "anthropic", "google")}
    for _ in range(30):
        with factory() as session:
            pending = list(session.scalars(select(Step.id).where(Step.run_id == run_id, Step.status == "pending")))
        with factory.begin() as session:
            advance_run(session, lock_work_run(session, run_id))
        for step_id in pending:
            elapse_retry_wait(factory, step_id)
            with factory() as session:
                step = session.get(Step, step_id)
                is_audit = step.stage == "audit"
                previous = session.get(Run, run_id).version
            execute_step(step_id, session_factory=factory, adapter_registry=registry)
            if is_audit:
                with factory() as session:
                    run = session.get(Run, run_id)
                    assert run.stage == "synthesis"
                    assert run.configuration["stage_iteration"] == 1
                    assert run.version == previous + 1
                return
    raise AssertionError("first audit was not scheduled")
