"""Offline audit reproductions; no product source changes or remote calls.

Run from repo root: PYTHONPATH=backend/src:. uv run python docs/evidence/algorithm-audit-2026-11-17/root/probe_protocol.py
"""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.tests.test_orchestration import seed, drain
from round_robin.api.preflight import input_packet_error
from round_robin.api.events import snapshot
from round_robin.database.models import Base, Run, Step, Upload, Attempt, ReportVersion
from round_robin.database.services import append_event
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from round_robin.worker.tasks import execute_step


def add_upload(factory, run_id, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    with factory.begin() as session:
        run = session.get(Run, run_id)
        upload = Upload(id="upload_selected", meeting_id=run.meeting_id, owner_id=run.owner_id,
            status="ready", filename="selected.txt", media_type="text/plain", canonical_text=text,
            sha256=digest, byte_size=len(text.encode()), recipients=["openai", "anthropic", "google"])
        session.add(upload)
        run.input_snapshot = {**run.input_snapshot, "selected_attachment_ids": [upload.id]}
        manifest = [{"id": upload.id, "sha256": digest, "recipients": upload.recipients,
            "byte_size": upload.byte_size}]
        run.configuration = {**run.configuration, "input_manifest": manifest}
        session.flush()
        return input_packet_error(session, run.meeting_id, run.owner_id, run.input_snapshot, manifest, [])


def attachment_visibility(factory):
    run_id = seed(factory)
    marker = "The selected attachment contains the decisive project restriction."
    assert add_upload(factory, run_id, marker) is None
    drain(factory)
    with factory() as session:
        attempts = list(session.scalars(select(Attempt).where(Attempt.run_id == run_id)))
        intake = next(item.request_json for item in attempts if item.request_json["stage"] == "intake")
        proposal = next(item.request_json for item in attempts if item.request_json["stage"] == "proposal")
        result = {"intake_selected_attachment_ids": intake["brief"]["selected_attachment_ids"],
            "intake_evidence_count": len(intake["evidence"]),
            "intake_contains_selected_text": marker in json.dumps(intake),
            "proposal_contains_selected_text": marker in json.dumps(proposal),
            "stages": [item.request_json["stage"] for item in attempts]}
        assert result["intake_contains_selected_text"] is False
        assert result["proposal_contains_selected_text"] is True
        return result


def repair_feedback(factory):
    run_id = seed(factory)
    packets = []
    class FailureOnce(SimulatedProvider):
        def generate(self, request):
            packets.append(request.packet())
            if len(packets) == 1:
                raise ProviderFailure("invalid_reference", retryable=True, cost_usd=Decimal("0"),
                    raw_text='{"reference_ids":["wrong_source_identity"]}')
            return super().generate(request)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    adapter = FailureOnce("openai")
    execute_step(step_id, session_factory=factory, adapter_registry={"openai": adapter})
    execute_step(step_id, session_factory=factory, adapter_registry={"openai": adapter})
    different = [key for key in packets[0] if packets[0][key] != packets[1][key]]
    assert different == ["repair"]
    return {"different_packet_fields": different, "retry_repair_flag": packets[1]["repair"],
        "rejected_reference_supplied": "wrong_source_identity" in json.dumps(packets[1]),
        "validation_error_supplied": "invalid_reference" in json.dumps(packets[1]),
        "packet_fields": sorted(packets[1])}


def unicode_packet_preflight(factory):
    run_id = seed(factory)
    # Valid UTF-8 upload, 20 passages. Counts fit; bytes exceed the request cap.
    text = "界" * 40000
    error = add_upload(factory, run_id, text)
    assert error is None
    drain(factory)
    with factory() as session:
        run = session.get(Run, run_id)
        steps = list(session.scalars(select(Step).where(Step.run_id == run_id)))
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content
        result = {"input_packet_error": error, "upload_characters": len(text),
            "upload_utf8_bytes": len(text.encode()), "status": run.status,
            "stop_reason": run.stop_reason,
            "steps": [{"stage": s.stage, "status": s.status, "error_code": s.error_code, "calls": s.calls} for s in steps],
            "recommendation": report["recommendation"]}
        assert any(step.error_code == "context_limit" for step in steps)
        assert report["recommendation"] is None
        return result


def event_snapshot_pagination(factory):
    run_id = seed(factory)
    drain(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        for index in range(110):
            append_event(session, run, "audit.synthetic_event", {"index": index})
        append_event(session, run, "audit.final_concern", {"message": "Late audit concern sentinel"})
    with factory() as session:
        result = snapshot(run_id, owner="owner_test", session=session)
        output = {"event_cutoff": result["event_cutoff"], "visible_event_count": len(result["events"]),
            "last_visible_sequence": result["events"][-1]["sequence"],
            "next_event_cursor": result["next_event_cursor"],
            "late_concern_visible_in_snapshot": "Late audit concern sentinel" in json.dumps(result["events"])}
        assert output["visible_event_count"] == 100 and output["next_event_cursor"] == 100
        assert not output["late_concern_visible_in_snapshot"]
        source = Path("frontend/app/live-workspace.tsx").read_text()
        output["client_consumes_next_event_cursor"] = "next_event_cursor" in source
        output["client_reads_events_endpoint"] = "/events?" in source
        assert not output["client_consumes_next_event_cursor"] and not output["client_reads_events_endpoint"]
        return output


if __name__ == "__main__":
    results = {}
    for probe in (attachment_visibility, repair_feedback, unicode_packet_preflight, event_snapshot_pagination):
        with TemporaryDirectory(prefix="round-robin-protocol-audit-") as temporary:
            engine = create_engine(f"sqlite:///{Path(temporary) / 'probe.sqlite'}", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            results[probe.__name__] = probe(factory)
            engine.dispose()
    output = Path(__file__).with_name("probe-results.json")
    output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
