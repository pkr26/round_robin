from dataclasses import asdict
from types import SimpleNamespace

import pytest
from round_robin.providers.images import ImageAuthorization
from round_robin.visual.iterations import VisualLimit, VisualState
from round_robin.visual.jobs import ImageJob, recover_saved_job


class LookupProvider:
    def __init__(self, result, persisted, auth):
        self.capabilities = SimpleNamespace(
            provider="openai", asynchronous_jobs=True, job_lookup=True
        )
        self.result, self.persisted, self.auth, self.calls = result, persisted, auth, []

    def lookup(self, job_id, authorization):
        assert authorization is self.auth
        assert self.persisted and self.persisted[-1]["poll_count"] == 1
        self.calls.append(job_id)
        return self.result


def setup_lookup(result):
    state = VisualState("improve", "1", 1000)
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
    persisted = []
    provider = LookupProvider(result, persisted, auth)

    def persist(value):
        assert isinstance(value, (VisualState, ImageJob))
        persisted.append(asdict(value))

    def lookup():
        return recover_saved_job(job, state, provider, auth, now=1001, persist=persist)

    return job, state, auth, provider, persisted, lookup


@pytest.mark.parametrize("damage", ["fence", "epoch", "provider"])
def test_restored_authority_mismatch_stops_lookup_without_persisting(damage):
    job, state, _, provider, persisted, lookup = setup_lookup({"status": "completed"})
    if damage == "fence":
        state.fence = 1
    elif damage == "epoch":
        state.epoch = 2
    else:
        provider.capabilities.provider = "anthropic"
    with pytest.raises(VisualLimit):
        lookup()
    assert state.poll_count == 0 and provider.calls == [] and persisted == []
    assert job.status == "pending"


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"status": "completed"}, "completed"),
        ({"status": "pending"}, "pending"),
        ({}, "unknown"),
    ],
)
def test_lookup_persists_count_before_same_authorized_job_and_records_outcome(
    result, expected
):
    job, state, auth, provider, persisted, lookup = setup_lookup(result)
    assert lookup() == result
    assert provider.calls == ["remote_a"]
    assert state.poll_count == 1 and len(persisted) == 2
    assert persisted[0]["poll_count"] == 1 and persisted[1]["id"] == job.id
    assert persisted[1]["status"] == job.status == expected
    assert auth.operation_id == "operation_a"


@pytest.mark.parametrize("damage", ["not_async", "no_lookup", "no_job_id"])
def test_unrecoverable_job_never_polls_or_resubmits(damage):
    job, state, _, provider, persisted, lookup = setup_lookup({"status": "completed"})
    if damage == "not_async":
        provider.capabilities.asynchronous_jobs = False
    elif damage == "no_lookup":
        provider.capabilities.job_lookup = False
    else:
        job.provider_job_id = None
    with pytest.raises(VisualLimit):
        lookup()
    assert state.poll_count == 0 and provider.calls == [] and persisted == []
