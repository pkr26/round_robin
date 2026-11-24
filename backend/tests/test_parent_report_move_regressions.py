import pytest
from round_robin.database.models import ReportVersion, Run
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import finalize_run
from sqlalchemy import delete, select, update

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


@pytest.mark.parametrize("action", ["move", "delete", "version"])
def test_prior_report_identity_is_cleared_when_row_changes_scope(
    factory, monkeypatch, action
):
    parent = seed(factory)
    drain(factory)
    child = seed(factory, suffix="child")
    other = seed(factory, suffix="outside")
    with factory.begin() as session:
        p = session.get(Run, parent)
        c = session.get(Run, child)
        c.parent_run_id = parent
        c.meeting_id = p.meeting_id
        parent_id = session.scalar(
            select(ReportVersion.id).where(ReportVersion.run_id == parent)
        )
    with factory.begin() as session:
        run = lock_work_run(session, child)
        original = session.scalar
        injected = []

        def alter_before_full_report(statement, *args, **kwargs):
            if (
                not injected
                and "SELECT report_versions.id, report_versions.run_id,"
                in str(statement)
            ):
                injected.append(True)
                if action == "move":
                    stmt = (
                        update(ReportVersion)
                        .where(ReportVersion.id == parent_id)
                        .values(run_id=other)
                    )
                elif action == "version":
                    stmt = (
                        update(ReportVersion)
                        .where(ReportVersion.id == parent_id)
                        .values(version=2)
                    )
                else:
                    stmt = delete(ReportVersion).where(ReportVersion.id == parent_id)
                session.execute(stmt.execution_options(synchronize_session=False))
            return original(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", alter_before_full_report)
        finalize_run(session, run)
        assert injected
        row = session.scalar(select(ReportVersion).where(ReportVersion.run_id == child))
        assert row
        report = row.content
        assert report["parent_report_id"] is None
        assert any(
            "prior report is unavailable" in x.lower() for x in report["change_summary"]
        )
        assert not any(
            "preferred-option result changed" in x or "Decision readiness changed" in x
            for x in report["change_summary"]
        )
