"""Fresh adversarial evidence, publication and arithmetic boundary checks."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, Inexact, localcontext
import json
from threading import Barrier

from hypothesis import given, strategies as st
from pydantic import ValidationError
import pytest
from sqlalchemy import event, select, update

from backend.tests.test_research_service import PageFixture, SearchFixture, research_run
from backend.tests.test_runtime_report import runtime_body
from backend.tests.test_orchestration import factory
from backend.tests.test_operations import prepared
from round_robin.database.models import EvidenceRecord, ReportVersion, Run
from round_robin.fixtures import sample_brief, sample_report
from round_robin.reports.exports import render_saved_report
from round_robin.reports.read_adapters import UnreadableReport, read_runtime_report
from round_robin.reports.readiness import derive_readiness
from round_robin.research.calculator import Operand, Operation, break_even, evaluate, lifecycle_total
from round_robin.research.fetcher import FetchAllowance, FetchRejected, FetchResponse, collect_source
from round_robin.research.service import execute_research, schedule_research
from round_robin.schemas.runtime import RuntimeDecisionReport
from round_robin.schemas.views import StoredEvidence


def test_successful_public_research_is_readable_through_the_typed_evidence_boundary(research_run):
    with research_run.begin() as session:
        assert schedule_research(session, session.get(Run, "run_local"))
    assert execute_research("run_local", session_factory=research_run, search=SearchFixture(), transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        saved = list(session.scalars(select(EvidenceRecord)))
        assert saved
        for row in saved:
            assert StoredEvidence.model_validate(row.content).sha256 == row.sha256
            with pytest.raises(ValidationError, match="hash must match"):
                StoredEvidence.model_validate({**row.content, "text": row.content["text"] + " changed"})


def test_untrusted_search_metadata_cannot_commit_postgresql_invalid_evidence(research_run):
    from round_robin.research.search import SearchHit
    class UnsafeTitle:
        def search(self, query, **kwargs):
            return [SearchHit("PRIVATE\x00TITLE", "https://example.com/quote")]
    with research_run.begin() as session:
        assert schedule_research(session, session.get(Run, "run_local"))
    assert execute_research("run_local", session_factory=research_run, search=UnsafeTitle(), transport=PageFixture(), resolver=lambda _: ["93.184.216.34"])
    with research_run() as session:
        assert not list(session.scalars(select(EvidenceRecord)))
        assert session.get(Run, "run_local").configuration["research_summary"]["status"] == "limited"


def test_untrusted_page_nul_is_rejected_before_it_can_reach_json_storage():
    class Page:
        def get(self, target, **kwargs):
            return FetchResponse(200, {"content-type": "text/plain"}, b"Public\x00text")
    with pytest.raises(FetchRejected):
        collect_source("candidate", FetchAllowance(registered_urls={"candidate": "https://example.com/"}), transport=Page(), resolver=lambda _: ["93.184.216.34"])


@pytest.mark.parametrize("field,value", [("known_subtotal", "999999.00"), ("missing_components", ["A hidden cost gap"])])
def test_saved_cost_summary_cannot_disagree_with_its_exact_calculation(runtime_body, field, value):
    body = deepcopy(runtime_body)
    body["comparison"][0][field] = value
    if field == "missing_components":
        body["comparison"][0]["total"] = None
        body["calculations"][0]["output"] = None
    with pytest.raises(ValidationError):
        RuntimeDecisionReport.model_validate(body)


@pytest.mark.parametrize("field,value", [("id", "report_other"), ("version", 999), ("event_cutoff", 999999)])
def test_saved_report_reader_binds_immutable_row_identity_and_cutoff(factory, runtime_body, field, value):
    with factory() as session:
        row = session.get(ReportVersion, runtime_body["id"])
        row.content = {**deepcopy(row.content), field: value}
        with pytest.raises(UnreadableReport):
            read_runtime_report(session, row)


def test_malformed_saved_scope_uses_safe_unreadable_report_error(factory, runtime_body):
    with factory() as session:
        row = session.get(ReportVersion, runtime_body["id"])
        row.content = {**deepcopy(row.content), "evidence_ids": [["invalid"]]}
        with pytest.raises(UnreadableReport):
            read_runtime_report(session, row)


def test_ready_hard_requirement_assessments_need_a_supporting_reference():
    report = sample_report(sample_brief(), scenario_id="base12")
    comparison = [item.model_copy(update={"constraint_results": [result.model_copy(update={"basis_refs": []}) for result in item.constraint_results]}) for item in report.comparison]
    readiness, _ = derive_readiness(selected_option_id=report.recommendation.option_id, comparison=comparison, dependencies=report.essential_dependencies, coverage=report.assessment, required_requirement_ids=set(report.hard_requirement_ids))
    assert readiness == "conditional"


def test_division_rounding_contract_is_independent_of_callers_decimal_traps():
    with localcontext() as ambient:
        ambient.traps[Inexact] = True
        operation = Operation(operation="divide", operands=[Operand(amount="1", kind="quantity", unit="dimensionless"), Operand(amount="3", kind="quantity", unit="dimensionless")])
        output, _, flags = evaluate(operation)
        assert Decimal(output) < 1 and flags
        first, second = sample_brief().options[:2]
        components = [component.model_copy(update={"amount": "21"}) if component.frequency == "monthly" else component for component in second.cost_components]
        result = break_even(first, second.model_copy(update={"cost_components": components}), "USD")
        assert result.output and result.flags


@given(cents=st.integers(min_value=0, max_value=10**10), quantity=st.integers(min_value=0, max_value=1000), months=st.integers(min_value=1, max_value=120))
def test_monthly_totals_match_exact_integer_cent_arithmetic(cents, quantity, months):
    option = sample_brief().options[0]
    component = option.cost_components[0].model_copy(update={"amount": format(Decimal(cents) / 100, ".2f"), "quantity": str(quantity), "frequency": "monthly"})
    calculated = lifecycle_total(option.model_copy(update={"cost_components": [component]}), months, "USD", calculation_id="calc_property")
    assert Decimal(calculated.output) == Decimal(cents * quantity * months) / 100


def test_markdown_retains_conditions_and_action_ownership_from_saved_report():
    report = sample_report(sample_brief(), scenario_id="base12").model_dump(mode="json")
    report["recommendation"]["conditions"] = ["Obtain installation approval before committing."]
    report["next_evidence"] = [{"question": "Confirm installation", "affected_conclusion": "Feasibility", "verification_method": "Site visit", "request_text": "Inspect the proposed site.", "responsible_person": "Facilities lead", "revisit_date": "2026-12-01"}]
    content, _ = render_saved_report(report, "markdown", title="Saved conditions")
    from html import unescape
    import re
    text = unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", content.decode()))
    assert report["recommendation"]["conditions"][0] in text
    assert "Facilities lead" in text and "2026-12-01" in text


def test_concurrent_first_logins_create_one_owner_and_two_usable_sessions(tmp_path):
    from backend.tests.test_hosted import hosted_settings
    from round_robin.app import create_app
    from round_robin.database.models import Owner, PolicyBucket
    from round_robin.security.hosted import create_session
    app = create_app(f"sqlite:///{tmp_path / 'first-login.db'}", settings=hosted_settings(tmp_path), initialize=True)
    start, owner_reads = Barrier(2), Barrier(2)
    def force_unserialized_first_reads(connection, cursor, statement, parameters, context, executemany):
        if "FROM owners" in statement and not connection.connection.driver_connection.in_transaction:
            owner_reads.wait(timeout=3)
    event.listen(app.state.engine, "after_cursor_execute", force_unserialized_first_reads)
    def login():
        start.wait(timeout=3)
        return create_session(app.state.session_factory, app.state.settings, "https://identity.example", "alice")
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(login) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        assert results[0][2] == results[1][2]
        assert results[0][0] != results[1][0]
    finally:
        event.remove(app.state.engine, "after_cursor_execute", force_unserialized_first_reads)
    with app.state.session_factory() as session:
        assert len(list(session.scalars(select(Owner)))) == 1
        assert len(list(session.scalars(select(PolicyBucket).where(PolicyBucket.key.like("session:%"))))) == 2


@pytest.mark.parametrize("query", ["host=remote.example", "hostaddr=198.51.100.20", "dbname=production", "service=remote-production", "port=9999"])
def test_restore_destination_cannot_be_overridden_by_driver_query_parameters(prepared, tmp_path, monkeypatch, query):
    from round_robin.operations import recovery
    app, _, _, _, settings = prepared
    manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
    archive = tmp_path / "restore_archive"
    recovery.backup(app.state.engine, settings.artifact_directory, archive, manifest)
    calls = []
    def forbidden_connection(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Unsafe restoration destination reached the engine")
    monkeypatch.setattr(recovery, "create_engine", forbidden_connection)
    with pytest.raises(recovery.OperationError, match="Refusing"):
        recovery.restore_verify(archive, f"postgresql+psycopg://localhost/disposable_restore_test?{query}", tmp_path / "restored", manifest)
    assert calls == []


@pytest.mark.parametrize("value", [3, 24, True, 2.0])
def test_finish_reservation_rejects_call_counts_the_runtime_cannot_preserve(value):
    from backend.tests.test_provider_configuration import synthetic_configuration
    from round_robin.schemas.provider_configuration import ProviderConfiguration
    configuration = synthetic_configuration()
    configuration["finish_policy"]["reserved_requests"] = value
    with pytest.raises(ValidationError):
        ProviderConfiguration.model_validate(configuration)


def test_sqlite_backup_reads_related_tables_from_one_physical_snapshot(prepared, tmp_path):
    from round_robin.database.models import Draft, Meeting
    from round_robin.operations.recovery import backup
    app, meeting, _, _, settings = prepared
    engine = app.state.engine
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    before = meeting["title"]
    changed = []
    def concurrent_revision(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT meetings.") and not changed:
            changed.append(True)
            with engine.begin() as writer:
                writer.execute(update(Meeting).where(Meeting.id == meeting["id"]).values(title="New atomic revision"))
                payload = dict(writer.execute(select(Draft.payload).where(Draft.meeting_id == meeting["id"])).scalar_one())
                payload["title"] = "New atomic revision"
                writer.execute(update(Draft).where(Draft.meeting_id == meeting["id"]).values(payload=payload))
    event.listen(engine, "after_cursor_execute", concurrent_revision)
    try:
        destination = tmp_path / "consistent_backup"
        backup(engine, settings.artifact_directory, destination, settings.artifact_directory.parent / "deletion-manifest.jsonl")
    finally:
        event.remove(engine, "after_cursor_execute", concurrent_revision)
    tables = json.loads((destination / "snapshot.json").read_text())["tables"]
    saved_meeting = next(row for row in tables["meetings"] if row["id"] == meeting["id"])
    saved_draft = next(row for row in tables["drafts"] if row["meeting_id"] == meeting["id"])
    assert changed
    assert saved_meeting["title"] == saved_draft["payload"]["title"] == before


def test_sqlite_backup_respects_an_already_open_pep249_transaction(tmp_path):
    from sqlalchemy import create_engine
    from round_robin.database.models import Base
    from round_robin.operations.durability import marker_pending
    from round_robin.operations.recovery import backup
    engine = create_engine(f"sqlite:///{tmp_path / 'pep249.db'}", connect_args={"autocommit": False})
    Base.metadata.create_all(engine)
    try:
        artifacts = tmp_path / "artifacts"
        result = backup(engine, artifacts, tmp_path / "pep249_backup", tmp_path / "deletion-manifest.jsonl")
        assert result["status"] == "backup_saved"
        assert not marker_pending(artifacts)
    finally:
        engine.dispose()
