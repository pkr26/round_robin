from round_robin.database.models import Run, Step
from round_robin.providers.base import ProviderFailure
from round_robin.providers.simulated import SimulatedProvider
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_unavailable_independent_audit_publishes_limited_result(factory):
    class UnavailableAuditor(SimulatedProvider):
        def generate(self, request):
            if request.stage == "audit":
                raise ProviderFailure("upstream_unavailable", retryable=False)
            return super().generate(request)

    run_id = seed(factory)
    registry = {provider: UnavailableAuditor(provider) for provider in ("openai", "anthropic", "google")}
    drain(factory, registry)
    with factory() as session:
        run = session.get(Run, run_id)
        audit = session.scalar(select(Step).where(Step.run_id == run_id, Step.stage == "audit"))
        assert audit.status == "failed"
        assert run.status == "completed_with_limitations"
        assert run.stop_reason == "independent_audit_incomplete_or_negative"
