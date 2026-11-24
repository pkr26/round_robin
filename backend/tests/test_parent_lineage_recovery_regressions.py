from copy import deepcopy

import pytest
from round_robin.database.models import ReportVersion, Run
from round_robin.fixtures import sample_brief
from round_robin.orchestration import controller
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import finalize_run
from round_robin.reports.read_adapters import read_runtime_report
from sqlalchemy import select, text

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def pair(factory, case):
    parent_id = seed(factory)
    drain(factory)
    child_brief = sample_brief().model_copy(
        update={"horizon_months": 24, "budget": "2000"}
    )
    child_id = seed(factory, brief=child_brief, suffix="child")
    with factory.begin() as session:
        parent = session.get(Run, parent_id)
        child = session.get(Run, child_id)
        child.parent_run_id = parent_id
        if case != "foreign_parent":
            child.meeting_id = parent.meeting_id
        if case == "foreign_owner":
            parent.owner_id = "foreign_owner"
        row = session.scalar(
            select(ReportVersion).where(ReportVersion.run_id == parent_id)
        )
        parent_report_id = row.id
        if case == "legacy":
            body = deepcopy(row.content)
            body.pop("evidence_ids", None)
            body.pop("contribution_ids", None)
            row.content = body
            session.flush()
            assert read_runtime_report(session, row)["id"] == parent_report_id
        elif case == "mismatched_identity":
            row.content = {
                **row.content,
                "id": "report_forged",
                "readiness": "ready_to_choose",
            }
        elif case == "invalid_readiness":
            row.content = {**row.content, "readiness": "PRIVATE_SENTINEL"}
        elif case == "mismatched_run":
            row.content = {**row.content, "run_id": "run_forged"}
        elif case == "bad_brief":
            parent.input_snapshot = ["corrupt"]
        elif case in ("raw_brief", "raw_configuration"):
            column = "input_snapshot" if case == "raw_brief" else "configuration"
            session.execute(
                text(f"UPDATE runs SET {column} = :raw WHERE id = :id"),
                {"raw": '{"PRIVATE_SENTINEL":', "id": parent_id},
            )
        elif case == "bad_recommendation":
            row.content = {**row.content, "recommendation": ["forged"]}
        elif case == "missing_report":
            session.delete(row)
    return parent_id, child_id, parent_report_id


def finalized(factory, child_id):
    with factory.begin() as session:
        finalize_run(session, lock_work_run(session, child_id))
        row = session.scalar(
            select(ReportVersion).where(ReportVersion.run_id == child_id)
        )
        return deepcopy(read_runtime_report(session, row))


def disclosure(report):
    texts = [
        text.lower()
        for text in report["change_summary"]
        + report["main_reasons"]
        + report["readiness_reasons"]
    ]
    return any(
        ("prior" in text or "parent" in text)
        and any(
            word in text
            for word in ("unavailable", "cannot", "could not", "not available")
        )
        for text in texts
    )


@pytest.mark.parametrize(
    "case",
    [
        "mismatched_identity",
        "mismatched_run",
        "invalid_readiness",
        "bad_recommendation",
    ],
)
def test_invalid_parent_content_preserves_authoritative_identity_without_fabricated_differences(
    factory, case
):
    _, child, parent_report_id = pair(factory, case)
    report = finalized(factory, child)
    assert report["parent_report_id"] == parent_report_id
    assert "PRIVATE_SENTINEL" not in str(report)
    assert disclosure(report)
    assert not any(
        "preferred-option result changed" in item
        or "Decision readiness changed" in item
        for item in report["change_summary"]
    )


def test_valid_legacy_parent_is_read_and_linked(factory):
    _, child, parent_report_id = pair(factory, "legacy")
    report = finalized(factory, child)
    assert report["parent_report_id"] == parent_report_id
    assert any("Budget changed" in item for item in report["change_summary"])
    assert any("horizon changed" in item for item in report["change_summary"])


def test_missing_parent_report_is_not_falsely_described_as_no_parent(factory):
    _, child, _ = pair(factory, "missing_report")
    report = finalized(factory, child)
    assert report["parent_report_id"] is None
    assert disclosure(report)


@pytest.mark.parametrize("case", ["foreign_parent", "foreign_owner"])
def test_foreign_parent_report_never_linked_or_quoted(factory, case):
    _, child, parent_report_id = pair(factory, case)
    report = finalized(factory, child)
    assert report["parent_report_id"] is None
    assert parent_report_id not in str(report)


def test_corrupt_parent_brief_does_not_invent_input_differences_or_lose_lineage(
    factory,
):
    _, child, parent_report_id = pair(factory, "bad_brief")
    report = finalized(factory, child)
    assert report["parent_report_id"] == parent_report_id
    assert disclosure(report)
    assert not any(
        "Budget changed" in item or "horizon changed" in item
        for item in report["change_summary"]
    )


@pytest.mark.parametrize("case", ["raw_brief", "raw_configuration"])
def test_raw_invalid_parent_run_json_does_not_lose_authoritative_lineage(factory, case):
    _, child, parent_report_id = pair(factory, case)
    report = finalized(factory, child)
    assert report["parent_report_id"] == parent_report_id
    assert "PRIVATE_SENTINEL" not in str(report)
    assert disclosure(report)


@pytest.mark.parametrize(
    "case", ["cancelled", "invalid_inputs", "cancelled_invalid_inputs"]
)
def test_unreadable_parent_preserves_child_partial_and_cancellation(factory, case):
    _, child, parent_report_id = pair(factory, "mismatched_identity")
    with factory.begin() as session:
        run = session.get(Run, child)
        if "cancelled" in case:
            run.status = "cancel_requested"
            run.stop_reason = "user_cancellation"
        if "invalid_inputs" in case:
            run.input_snapshot = {"PRIVATE_SENTINEL": "invalid"}
    report = finalized(factory, child)
    assert report["parent_report_id"] == parent_report_id
    assert disclosure(report)
    assert "PRIVATE_SENTINEL" not in str(report)
    assert report["execution_status"] == (
        "cancelled" if "cancelled" in case else "failed"
    )
    if "cancelled" in case:
        assert report["stop_reason"] == "user_cancellation"
    if "invalid_inputs" in case:
        assert report["comparison"] == [] and report["readiness"] is None


@pytest.mark.parametrize("field", ["owner_id", "meeting_id"])
def test_scope_change_before_final_recheck_discards_all_parent_context(
    factory, monkeypatch, field
):
    from sqlalchemy import update

    parent, child, _ = pair(factory, "healthy")
    with factory.begin() as session:
        child_run = lock_work_run(session, child)
        scalar = session.scalar
        triggered = False

        def scalar_with_scope_change(statement, *args, **kwargs):
            nonlocal triggered
            selected = list(getattr(statement, "selected_columns", []))
            if (
                not triggered
                and len(selected) == 1
                and getattr(selected[0], "key", None) == "id"
                and getattr(getattr(selected[0], "table", None), "name", None) == "runs"
            ):
                triggered = True
                session.execute(
                    update(Run)
                    .where(Run.id == parent)
                    .values({field: "foreign_scope"})
                    .execution_options(synchronize_session=False)
                )
            return scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", scalar_with_scope_change)
        (
            parent_id,
            parent_report,
            parent_brief,
            report_unavailable,
            brief_unavailable,
        ) = controller._parent_context(session, child_run)
        assert triggered
        assert (parent_id, parent_report, parent_brief) == (None, None, None)
        assert report_unavailable and brief_unavailable
