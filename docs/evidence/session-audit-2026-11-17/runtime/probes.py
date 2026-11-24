"""Current-source offline runtime audit; no product mutation or external I/O.

Run: PYTHONPATH=backend/src:. uv run python docs/evidence/session-audit-2026-11-17/runtime/probes.py
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.tests.test_orchestration import seed
from backend.tests.test_provider_configuration import synthetic_configuration
from round_robin.api.preflight import input_packet_error
from round_robin.database.models import Base, Run, Step
from round_robin.fixtures import sample_brief
from round_robin.orchestration.admission import lock_work_run, provider_capacity_available
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider
from round_robin.providers.simulated import SimulatedProvider
from round_robin.schemas.briefs import DraftBrief
from round_robin.worker.image_tasks import _capacity
from round_robin.worker.tasks import build_request, execute_step


def context_and_capacity(factory):
    body = sample_brief().model_dump(mode="json")
    body.update(background="x" * 12000, goal="g" * 4000, question="q" * 4000)
    run_id = seed(factory, brief=DraftBrief.model_validate(body))
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run.id))
        request = build_request(session, run, step)
        preflight_error = input_packet_error(session, run.meeting_id, run.owner_id, run.input_snapshot, [], [])
        adapter = HTTPProvider("openai", {**synthetic_configuration(), "mode": "live"}, api_key="synthetic")
        try:
            adapter.conservative_bound(request)
            result = "fits"
        except ProviderFailure as failure:
            result = failure.code
        assert preflight_error is None and result == "context_limit"
        context = {"valid_brief": True, "input_packet_error": preflight_error,
            "canonical_intake_packet_bytes": len(json.dumps(request.packet(), ensure_ascii=False, separators=(",", ":")).encode()),
            "initial_packet_limit": 48000, "configured_verified_context_window": 32768,
            "actual_adapter_pre_send_admission": result,
            "network_calls": 0}
        run.stage = "synthesis"
        operation = SimpleNamespace(id="probe_optional_generation", provider="openai", payload={"kind": "generation"})
        capacity = {"running_text_stage": run.stage,
            "optional_text_call_allowed": provider_capacity_available(session, "openai"),
            "optional_image_generation_allowed": _capacity(session, SimpleNamespace(), operation),
            "network_calls": 0}
        assert capacity["optional_text_call_allowed"] is False and capacity["optional_image_generation_allowed"] is True
        return context, capacity


def fallback_order(factory):
    run_id = seed(factory, suffix="fallback")
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
        identity = session.scalar(select(Step.id).where(Step.run_id == run_id))
    invocations = []
    class MissingConfiguration(SimulatedProvider):
        def conservative_bound(self, request):
            invocations.append(self.provider)
            raise ProviderFailure("provider_configuration_invalid", may_have_sent=False)
    registry = {"openai": MissingConfiguration("openai"), "anthropic": MissingConfiguration("anthropic"), "google": SimulatedProvider("google")}
    results = [execute_step(identity, session_factory=factory, adapter_registry=registry) for _ in range(3)]
    with factory() as session:
        step, run = session.get(Step, identity), session.get(Run, run_id)
        result = {"declared_fallbacks": run.configuration["fallbacks"]["intake"],
            "pre_send_admission_sequence": invocations, "healthy_google_generate_calls": registry["google"].calls,
            "step_status": step.status, "step_calls": step.calls,
            "infrastructure_claims": step.infrastructure_claims, "run_status": run.status,
            "execute_results": results, "network_calls": 0}
        assert invocations == ["openai", "anthropic", "openai"]
        assert registry["google"].calls == 0 and step.calls == 0 and step.status == "failed"
        return result


if __name__ == "__main__":
    results = {}
    for name in ("context_and_capacity", "fallback_order"):
        with TemporaryDirectory(prefix="session-runtime-audit-") as temporary:
            engine = create_engine(f"sqlite:///{temporary}/audit.sqlite", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            if name == "context_and_capacity":
                results["preflight_context"], results["cross_modal_priority"] = context_and_capacity(factory)
            else:
                results[name] = fallback_order(factory)
            engine.dispose()
    path = Path(__file__).with_name("probe-results.json")
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
