from dataclasses import asdict
from types import SimpleNamespace

import pytest
from round_robin.providers.images import ImageAuthorization
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.jobs import ImageJob, recover_saved_job


class LookupProvider:
    def __init__(self):
        self.capabilities = SimpleNamespace(
            provider="openai", asynchronous_jobs=True, job_lookup=True
        )
        self.calls = []

    def lookup(self, job_id, authorization):
        self.calls.append((job_id, authorization.operation_id))
        return {"status": "pending"}


def fixture(now, poll_count=0):
    state = VisualState("improve", "1", 1000, poll_count=poll_count)
    job = ImageJob(
        "job_a",
        "operation_a",
        "step_a",
        "openai",
        "hash_a",
        0,
        1,
        "owner_a",
        "session_a",
        "0.4",
        status="pending",
        provider_job_id="remote_a",
    )
    auth = ImageAuthorization(
        "operation_a", "owner_a", "session_a", "0.4", 0, 1, step_id="step_a"
    )
    provider = LookupProvider()
    persisted = []

    def lookup():
        return recover_saved_job(
            job,
            state,
            provider,
            auth,
            now=now,
            persist=lambda item: persisted.append(asdict(item)),
        )

    return state, job, provider, persisted, lookup


@pytest.mark.parametrize("now", [1000, 2799.999])
def test_valid_boundary_lookup_is_counted_before_remote_call(now):
    state, _job, provider, persisted, lookup = fixture(now, 59)
    assert lookup() == {"status": "pending"}
    assert state.poll_count == 60 and provider.calls == [("remote_a", "operation_a")]
    assert persisted[0]["poll_count"] == 60 and persisted[1]["status"] == "pending"


@pytest.mark.parametrize(
    "now", [999.999, float("nan"), float("inf"), -float("inf"), 2800, 2800.5]
)
def test_expired_or_invalid_clock_never_dispatches_lookup(now):
    state, job, provider, persisted, lookup = fixture(now, 12)
    with pytest.raises(VisualLimit):
        lookup()
    assert state.poll_count == 12 and provider.calls == [] and persisted == []
    assert job.status == "pending" and job.provider_job_id == "remote_a"


def test_exhausted_poll_limit_never_dispatches_lookup():
    state, _, provider, persisted, lookup = fixture(1001, 60)
    with pytest.raises(VisualLimit):
        lookup()
    assert state.poll_count == 60 and provider.calls == [] and persisted == []


@pytest.mark.parametrize(
    "status", ["cancelled", "completed", "quarantined", "deleted", "expired"]
)
def test_public_poll_reducer_rejects_terminal_state(status):
    state = VisualState("improve", "1", 1000, status=status)
    with pytest.raises(VisualLimit):
        state.poll(now=1001)
    assert state.poll_count == 0
