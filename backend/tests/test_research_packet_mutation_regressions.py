"""Research admission measures the real downstream proposal packet."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from round_robin.database.models import BudgetOperation, Run
from round_robin.fixtures import sample_bundle
from round_robin.orchestration import reporting
from round_robin.providers import base
from round_robin.research import discussion, reuse, service
from sqlalchemy import func, select

pytest_plugins = ("test_research_service",)


def test_proposal_size_counts_resolved_brief_and_scoped_packet_components(monkeypatch):
    initial = sample_bundle().brief.model_dump(mode="json")
    resolved = {**initial, "background": "resolved café context"}
    run = SimpleNamespace(id="run_synthetic", input_snapshot=initial, configuration={"resolved_brief": resolved})
    session = object()
    passages = [{"id": "evidence_one", "text": "public evidence"}]
    calculations = [{"id": "calc_one", "output": "42"}]
    imported_claims = [{"id": "claim_one", "proposition": "imported"}]
    assessments = [{"id": "assessment_one", "result": "supported"}]

    def compare(brief, *, as_of=None):
        assert brief.background == resolved["background"]
        assert as_of == expected_date
        return [], calculations

    def claims(actual_session, actual_run, *, imported_only=False):
        assert actual_session is session and actual_run is run and imported_only is True
        return imported_claims

    def scoped_assessments(actual_run, evidence, *, now):
        assert now == instant
        assert actual_run is run and evidence == {"evidence_one": passages[0]}
        return assessments

    instant = datetime(2025, 4, 6, 1, tzinfo=UTC)
    expected_date = date(2025, 4, 5)
    monkeypatch.setattr(service, "utcnow", lambda: instant)
    monkeypatch.setattr(reporting, "comparison_data", compare)
    monkeypatch.setattr(reuse, "claim_packet", claims)
    monkeypatch.setattr(discussion, "current_assessments", scoped_assessments)
    expected_packet = {
        "stage": "proposal", "role": "anthropic", "brief": resolved,
        "evidence": passages, "peer_outputs": [],
        "allowed_reference_ids": ["calc_one", "claim_one", "evidence_one"],
        "calculations": calculations, "claims": imported_claims,
        "cost_as_of": expected_date.isoformat(),
        "repair": False, "repair_diagnostics": [], "open_objections": [], "assessments": assessments,
    }
    expected_size = len(json.dumps(expected_packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    assert service.proposal_packet_bytes(session, run, passages) == expected_size


def test_resolved_brief_exceeding_context_reserve_prevents_public_search(research_run, monkeypatch):
    with research_run.begin() as session:
        run = session.get(Run, "run_local")
        assert service.schedule_research(session, run)
        original_size = service.proposal_packet_bytes(session, run, [])
        resolved = {**run.input_snapshot, "background": "é" * 1200}
        run.configuration = {**run.configuration, "resolved_brief": resolved}
        resolved_size = service.proposal_packet_bytes(session, run, [])
        assert resolved_size > original_size + 700
    monkeypatch.setattr(base, "MAX_INITIAL_PACKET_BYTES", original_size + 700)

    class NoSearch:
        def search(self, *args, **kwargs):
            pytest.fail("A full downstream packet must not authorize public search")

    assert service.execute_research("run_local", session_factory=research_run, search=NoSearch())
    with research_run() as session:
        run = session.get(Run, "run_local")
        assert run.configuration["research_summary"]["reason"] == "downstream_context_reserve"
        assert run.configuration["research_summary"]["queries"] == 0
        assert session.scalar(select(func.count()).select_from(BudgetOperation)) == 0


def test_oversized_proposal_packet_is_rejected_during_measurement(research_run, monkeypatch):
    with research_run() as session:
        run = session.get(Run, "run_local")
        packet_size = service.proposal_packet_bytes(session, run, [])
        monkeypatch.setattr(base, "MAX_INITIAL_PACKET_BYTES", packet_size - 1)
        with pytest.raises(base.ProviderFailure) as error:
            service.proposal_packet_bytes(session, run, [])
        assert error.value.code == "context_limit"
