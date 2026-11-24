from round_robin.database.models import Step
from round_robin.fixtures import sample_brief
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from sqlalchemy import select

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


def test_four_candidates_are_admitted_at_proposals(factory):
    brief = sample_brief()
    candidate = next(option for option in brief.options if option.role == "candidate")
    options = [candidate.model_copy(update={"id": f"candidate_{index}", "cost_components": []}) for index in range(4)]
    run_id = seed(factory, brief=brief.model_copy(update={"options": options}))
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.stage = "proposals"
        advance_run(session, run)
        assert run.status == "running", run.stop_reason
        assert run.configuration["evidence_prepared"] is True
        assert len(list(session.scalars(select(Step).where(Step.run_id == run_id, Step.stage == "proposal")))) == 3


def test_missing_horizon_cannot_start_proposals(factory):
    brief = sample_brief().model_copy(update={"horizon_months": None})
    run_id = seed(factory, brief=brief)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        run.stage = "proposals"
        advance_run(session, run)
        assert run.stop_reason == "confirmed_brief_incomplete"
        assert not list(session.scalars(select(Step).where(Step.run_id == run_id, Step.stage == "proposal")))


def test_intake_to_proposals_advances_run_version_once(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "intake"))
        step.status = "succeeded"
        step.result_json = {"output": {"questions": [], "proposed_changes": []}}
        previous = run.version

        advance_run(session, run)

        assert run.stage == "proposals"
        assert run.version == previous + 1
