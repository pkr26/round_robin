from round_robin.database.models import ReportVersion, Run
from round_robin.fixtures import sample_brief
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import finalize_run
from sqlalchemy import select, update

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_parent_moved_out_of_scope_before_brief_read_cannot_inform_comparison(
    factory, monkeypatch
):
    parent = seed(factory)
    drain(factory)
    child = seed(
        factory,
        suffix="child",
        brief=sample_brief().model_copy(update={"budget": "2000"}),
    )
    outside = seed(factory, suffix="outside")
    with factory.begin() as session:
        parent_row = session.get(Run, parent)
        child_row = session.get(Run, child)
        other = session.get(Run, outside)
        child_row.parent_run_id = parent
        child_row.meeting_id = parent_row.meeting_id
        foreign_meeting = other.meeting_id
    with factory.begin() as session:
        run = lock_work_run(session, child)
        original = session.scalar
        injected = []

        def scalar_with_move(statement, *args, **kwargs):
            if not injected and "SELECT runs.input_snapshot" in str(statement):
                injected.append(True)
                session.execute(
                    update(Run)
                    .where(Run.id == parent)
                    .values(meeting_id=foreign_meeting)
                    .execution_options(synchronize_session=False)
                )
            return original(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", scalar_with_move)
        finalize_run(session, run)
        assert injected
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == child))
        assert row
        report = row.content
        assert not any("Budget changed" in item for item in report["change_summary"]), (
            report["change_summary"]
        )
        assert report["parent_report_id"] is None
        assert any("unavailable" in item.lower() for item in report["change_summary"])
