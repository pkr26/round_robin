import pytest
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    ReportVersion,
    Run,
    Step,
)
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.worker import tasks
from sqlalchemy import select, text

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


@pytest.mark.parametrize("task_name", ["advance_task", "finalize_task"])
@pytest.mark.parametrize(
    "raw", ['{"PRIVATE_SENTINEL":', "[" * 10000 + "0" + "]" * 10000]
)
def test_standalone_task_fences_unreadable_step_without_new_send(
    factory, tmp_path, monkeypatch, task_name, raw
):
    factory.configure(
        info={
            "artifact_directory": tmp_path / "artifacts",
            "durability_control_directory": tmp_path / "control",
        }
    )
    rid = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, rid))
        sid = session.scalar(
            select(Step.id).where(Step.run_id == rid, Step.status == "pending")
        )
        assert sid
        session.execute(
            text("UPDATE steps SET result_json=:raw WHERE id=:id"),
            {"raw": raw, "id": sid},
        )
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    result = getattr(tasks, task_name)(rid)
    assert result["status"] in {"advanced", "finalized"}
    with factory() as session:
        run = session.get(Run, rid)
        assert (
            run.status == "failed" and run.stop_reason == "saved_state_integrity_failed"
        )
        assert run.configuration.get("saved_state_unavailable") is True
        assert session.get(BudgetOperation, f"finish_{rid}").status == "held"
        assert session.scalar(select(Attempt.id).where(Attempt.run_id == rid)) is None
        assert (
            session.scalar(select(ReportVersion.id).where(ReportVersion.run_id == rid))
            is None
        )
