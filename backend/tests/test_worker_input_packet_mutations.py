"""Worker packets use scoped, verified and stage-appropriate inputs."""

import hashlib
import socket
from types import SimpleNamespace

import pytest
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import EvidenceRecord, Run, Step, Upload, utcnow
from round_robin.database.services import canonical_hash
from round_robin.fixtures import sample_brief
from round_robin.orchestration.budget import BudgetDenied
from round_robin.research.reuse import StoredClaim
from round_robin.worker.tasks import build_request
from sqlalchemy import select

from backend.tests.test_orchestration import seed

pytest_plugins = ("backend.tests.test_orchestration",)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network calls forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def step(stage):
    return SimpleNamespace(
        id="request_step",
        stage=stage,
        stage_iteration=0,
        role_slot_id=stage,
        calls=0,
        error_code=None,
    )


def test_provider_sees_resolved_brief_once_evidence_prepared(factory):
    run_id = seed(factory, suffix="resolved")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        resolved = {**run.input_snapshot, "goal": "Synthetic confirmed goal"}
        assert run.input_snapshot["goal"] != resolved["goal"]
        run.configuration = {
            **run.configuration,
            "resolved_brief": resolved,
            "evidence_prepared": True,
        }
    with factory() as session:
        for stage in ("intake", "proposal"):
            request = build_request(session, session.get(Run, run_id), step(stage))
            assert request.brief["goal"] == "Synthetic confirmed goal"


def test_initial_intake_sees_selected_attachment_without_freezing_it(factory):
    text = "Synthetic delivery quote: 42 days."
    digest = hashlib.sha256(text.encode()).hexdigest()
    brief = sample_brief().model_copy(
        update={"selected_attachment_ids": ["upload_test"]}
    )
    run_id = seed(factory, brief=brief, suffix="intake")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        session.add(
            Upload(
                id="upload_test",
                meeting_id=run.meeting_id,
                owner_id=run.owner_id,
                filename="quote.txt",
                media_type="text/plain",
                byte_size=len(text.encode()),
                status="ready",
                canonical_text=text,
                sha256=digest,
                recipients=["openai", "anthropic", "google"],
            )
        )
        run.configuration = {
            **run.configuration,
            "input_manifest": [{"id": "upload_test", "sha256": digest}],
        }
    with factory() as session:
        run = session.get(Run, run_id)
        request = build_request(session, run, step("intake"))
        assert any(item["text"] == text for item in request.evidence)
        assert not run.configuration.get("evidence_prepared")
        assert "resolved_brief" not in run.configuration
        assert (
            session.scalar(
                select(EvidenceRecord).where(EvidenceRecord.run_id == run_id)
            )
            is None
        )


def put_claim(session, run, identity, producer_id):
    content = {
        "id": identity,
        "version": 1,
        "proposition": "Synthetic assumption",
        "kind": "assumption",
        "evidence_status": "not_checked",
        "reference_ids": [],
        "time_sensitive": False,
        "verified_at": utcnow().isoformat(),
    }
    content = StoredClaim.model_validate(content).model_dump(
        mode="json", exclude_unset=True
    )
    session.add(
        ClaimVersion(
            id=identity,
            version=1,
            run_id=run.id,
            meeting_id=run.meeting_id,
            owner_id=run.owner_id,
            step_id=producer_id,
            sha256=canonical_hash(content),
            content=content,
        )
    )


def test_proposals_only_see_imported_claims_and_synthesis_sees_current_claims(factory):
    run_id = seed(factory, suffix="claims")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        producer = Step(
            id="prior_proposal",
            run_id=run_id,
            stage="proposal",
            role_slot_id="proposal",
            provider="openai",
            status="succeeded",
            result_json={"output": {"summary": "Synthetic"}},
        )
        session.add(producer)
        session.flush()
        put_claim(session, run, "imported_claim", None)
        put_claim(session, run, "current_claim", producer.id)
    with factory() as session:
        run = session.get(Run, run_id)
        proposal = build_request(session, run, step("proposal"))
        synthesis = build_request(session, run, step("synthesis"))
        assert {claim["id"] for claim in proposal.claims} == {"imported_claim"}
        assert {claim["id"] for claim in synthesis.claims} == {
            "imported_claim",
            "current_claim",
        }
        assert "current_claim" not in proposal.allowed_reference_ids


def test_corrupted_imported_claim_fails_closed(factory):
    run_id = seed(factory, suffix="corrupt_claim")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        put_claim(session, run, "bad_claim", None)
        session.flush()
        session.get(ClaimVersion, "bad_claim").sha256 = "0" * 64
    with (
        factory() as session,
        pytest.raises(BudgetDenied, match="^claim_integrity_failed$"),
    ):
        build_request(session, session.get(Run, run_id), step("proposal"))
