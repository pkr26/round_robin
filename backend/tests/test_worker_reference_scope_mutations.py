"""A worker may cite only checked references in its own request packet."""

import hashlib
import socket
from types import SimpleNamespace

import pytest
from round_robin.database.models import EvidenceRecord, Run, Step, utcnow
from round_robin.providers.base import ProviderFailure, ProviderOutput, validate_output
from round_robin.research.reuse import persist_claims
from round_robin.worker.tasks import build_request

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


def output(refs):
    return ProviderOutput(summary="Synthetic citation test", recommendation_option_id=None,
        reference_ids=list(refs), questions=[], assumptions=[], constraints=[], audit_passed=None,
        critical_defects=[], dissent=[])


def test_packet_and_provider_validation_bind_all_and_only_own_references(factory):
    run_id = seed(factory, suffix="reference")
    foreign_id = seed(factory, suffix="foreign")
    text = "Synthetic evidence with a scoped source."
    digest = hashlib.sha256(text.encode()).hexdigest()
    with factory.begin() as session:
        run = session.get(Run, run_id)
        for identity, owner_run in (("evidence_local", run), ("evidence_foreign", session.get(Run, foreign_id))):
            content = {"id": identity, "run_id": owner_run.id, "owner_id": owner_run.owner_id,
                "source_title": "Synthetic source", "text": text, "sha256": digest, "origin": "user_supplied",
                "verification_status": "supported", "verified_at": utcnow().isoformat(), "time_sensitive": False,
                "origin_group": identity, "locator": "lines 1-2", "scope": "Synthetic source"}
            session.add(EvidenceRecord(id=identity, run_id=owner_run.id, meeting_id=owner_run.meeting_id,
                owner_id=owner_run.owner_id, sha256=digest, content=content))
        peer = Step(id="step_proposal", run_id=run_id, stage="proposal", role_slot_id="proposal", provider="openai",
            status="succeeded", result_json={"output": {"summary": "Synthetic proposal"}})
        session.add(peer)
        session.flush()
        committed = persist_claims(session, run, peer, {"claims": [{"alias": "new_claim_1",
            "proposition": "Synthetic supported claim", "kind": "fact", "evidence_status": "supported",
            "reference_ids": ["evidence_local"]}]}, {"evidence_local"})
        claim_id = committed["claims"][0]["id"]
    step = SimpleNamespace(id="step_synthesis", stage="synthesis", stage_iteration=0,
        role_slot_id="synthesis", calls=0, error_code=None)
    with factory() as session:
        request = build_request(session, session.get(Run, run_id), step)
    expected = {"evidence_local", claim_id, "step_proposal"} | {item["id"] for item in request.calculations}
    assert request.calculations and request.claims and request.evidence and request.peer_outputs
    assert request.allowed_reference_ids == expected
    assert set(request.packet()["allowed_reference_ids"]) == expected
    assert validate_output(output(expected), request).reference_ids
    with pytest.raises(ProviderFailure) as failure:
        validate_output(output(["evidence_foreign"]), request)
    assert failure.value.code == "invalid_reference"
